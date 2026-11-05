"""
Голосовые фильтры реального времени — LP-PSOLA.

Как это работает (и почему звучит чисто)
────────────────────────────────────────
1. Голос раскладывается на два независимых слоя (модель «источник — фильтр»):
     • фильтр  — спектральная огибающая голосового тракта (форманты, тембр),
                 оценивается линейным предсказанием (LPC);
     • источник — остаток предсказания: чистая последовательность импульсов
                 голосовых связок (для гласных) или шум (для согласных).
2. ВЫСОТА меняется только у источника — методом PSOLA: каждый импульс
   вырезается вместе с окрестностью в один период, окном Ханна, и
   переставляется с новым шагом. Фазы импульсов при этом не расходятся
   (в отличие от фазового вокодера), поэтому нет «металла» и «плывущего» звука.
3. ФОРМАНТЫ сдвигаются отдельно: полюса LPC-фильтра поворачиваются на нужный
   коэффициент (formant), и источник пропускается через новый фильтр.
   Высота и тембр независимы: «бурундук» не звучит как ускоренная плёнка,
   «Вейдер» — не как замедленная.
4. Глухие звуки (с, ш, т, п, дыхание) высоту не имеют — они проходят мимо
   PSOLA без изменений, поэтому согласные остаются чёткими и разборчивыми.

Что улучшено по сравнению с предыдущей версией
──────────────────────────────────────────────
  • окно LPC 30 мс вместо 15 мс — точнее оцениваются форманты на низких голосах;
  • порядок LPC 20 вместо 32 — меньше «подгонки» под гармоники тона,
    чище остаток, меньше «металла»;
  • метки основного тона уточняются кросс-корреляцией ПО ОСТАТКУ,
    а не по исходной волне — метки не зависят от формант;
  • корректная инициализация состояния IIR при синтезе — исчезают щелчки;
  • мягче AGC (0.33…3.0) — не «качает»;
  • защита от деления на почти-ноль в OLA — не «взрывается» в паузах;
  • задержка 42 мс — хватает на 2 периода даже для 70 Гц.

Интерфейс (не менять — на него завязаны audio_engine.py и ui_settings.py):
    VoiceFilterBank(sample_rate).set_filter(id) / .active / .process(chunk)
process() принимает float32-чанк (любой длины, обычно 960 при 48 кГц, моно) и
возвращает float32 той же длины. Фильтр ставится ПОСЛЕ шумоподавления и ДО
кодирования.
"""

import math

import numpy as np
from scipy import signal
from scipy.linalg import solve_toeplitz

SAMPLE_RATE = 48000

FILTER_NONE     = "none"
FILTER_CHIPMUNK = "chipmunk"
FILTER_VADER    = "vader"
FILTER_WHISPER  = "whisper_low"
FILTER_AUTOTUNE = "autotune"
FILTER_DEMON    = "demon"

FILTER_LABELS = {
    FILTER_NONE:     "— выключен —",
    FILTER_CHIPMUNK: "🐿 Бурундук (Chipmunk)",
    FILTER_VADER:    "🖤 Дарт Вейдер",
    FILTER_WHISPER:  "🤫 Низкий шёпот",
    FILTER_AUTOTUNE: "🎤 Автотюн",
    FILTER_DEMON:    "👹 Демон",
}

FILTER_ORDER = [
    FILTER_NONE, FILTER_CHIPMUNK, FILTER_VADER,
    FILTER_WHISPER, FILTER_AUTOTUNE, FILTER_DEMON,
]

# ── Параметры эффектов ───────────────────────────────────────────────────
#   voices  — список (коэффициент высоты, громкость); >1 выше, <1 ниже
#   formant — сдвиг формант; >1 «меньше/ярче», <1 «больше/глубже»
_SPECS = {
    FILTER_CHIPMUNK: dict(mode="psola",    voices=[(1.50, 1.00)], formant=1.20),
    FILTER_VADER:    dict(mode="psola",    voices=[(0.72, 1.00)], formant=0.82),
    FILTER_WHISPER:  dict(mode="whisper",  voices=[],             formant=0.92),
    FILTER_AUTOTUNE: dict(mode="autotune", voices=[(1.00, 1.00)], formant=1.00),
    FILTER_DEMON:    dict(mode="psola",    voices=[(0.62, 1.00), (0.31, 0.55)],
                          formant=0.78),
}


# ═════════════════════════════════════════════════════════════════════════
#  Скользящие буферы с абсолютной индексацией
# ═════════════════════════════════════════════════════════════════════════
class _Buf:
    """Скользящий буфер с абсолютной индексацией сэмплов."""

    def __init__(self, keep):
        self.keep = int(keep)
        self.a = np.zeros(self.keep)
        self.base = -self.keep

    @property
    def end(self):
        return self.base + len(self.a)

    def append(self, x):
        if len(x) == 0:
            return
        self.a = np.concatenate([self.a, x])
        if len(self.a) > 2 * self.keep:
            d = len(self.a) - self.keep
            self.a = self.a[d:].copy()
            self.base += d

    def truncate(self, abs_end):
        k = abs_end - self.base
        if 0 <= k < len(self.a):
            self.a = self.a[:k]

    def get(self, s, e):
        """[s, e) в абсолютных индексах; за пределами буфера — нули."""
        n = e - s
        if n <= 0:
            return np.zeros(0)
        lo, hi = s - self.base, e - self.base
        if lo >= 0 and hi <= len(self.a):
            return self.a[lo:hi]
        out = np.zeros(n)
        a0, a1 = max(lo, 0), min(hi, len(self.a))
        if a1 > a0:
            out[a0 - lo:a1 - lo] = self.a[a0:a1]
        return out


class _OLA:
    """Накопитель overlap-add с абсолютной индексацией."""

    def __init__(self, base):
        self.base = int(base)
        self.a = np.zeros(4096)

    def add(self, start, vec):
        if len(vec) == 0:
            return
        i = start - self.base
        if i < 0:
            vec = vec[-i:]
            i = 0
            if len(vec) == 0:
                return
        j = i + len(vec)
        if j > len(self.a):
            self.a = np.concatenate([self.a, np.zeros(max(j - len(self.a), 4096))])
        self.a[i:j] += vec

    def take(self, n):
        if len(self.a) < n + 2048:
            self.a = np.concatenate([self.a, np.zeros(n + 4096)])
        out = self.a[:n].copy()
        self.a = self.a[n:]
        self.base += n
        return out


# ── окна ─────────────────────────────────────────────────────────────────
_hann_cache = {}
_ramp_cache = {}


def _hann(L):
    """Ханн длиной 2L+1 с пиком 1 в центре.
    При OLA с шагом L даёт ровно 1 в сумме (свойство Hann)."""
    w = _hann_cache.get(L)
    if w is None:
        w = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(2 * L + 1) / (2.0 * L))
        _hann_cache[L] = w
    return w


def _ramp(L):
    """Нарастание 0→1 длиной L (косинусное). Спад = 1 − ramp."""
    r = _ramp_cache.get(L)
    if r is None:
        r = 0.5 - 0.5 * np.cos(np.pi * np.arange(L) / float(L))
        _ramp_cache[L] = r
    return r


# ═════════════════════════════════════════════════════════════════════════
#  LPC: оценка огибающей, сдвиг формант
# ═════════════════════════════════════════════════════════════════════════
class _Lpc:
    def __init__(self, sr, order, win_len):
        self.p = int(order)
        self.wl = int(win_len)
        self.win = np.hanning(self.wl + 2)[1:-1]
        self.nfft = 1 << int(math.ceil(math.log2(2 * self.wl)))
        k = np.arange(self.p + 1)
        # окно по лагам (сглаживает пики) + расширение полосы пропускания
        self.lagwin = np.exp(-0.5 * (2.0 * np.pi * 50.0 / sr * k) ** 2)
        self.gamma = 0.995 ** k
        self.ident = np.zeros(self.p + 1)
        self.ident[0] = 1.0
        # для варпа
        self.nw = 1024
        self.widx = np.arange(self.nw // 2 + 1, dtype=np.float64)
        self.wlag = np.exp(-0.5 * (2.0 * np.pi * 30.0 / sr * k) ** 2)

    def analyse(self, seg):
        xw = seg * self.win
        X = np.fft.rfft(xw, self.nfft)
        r = np.fft.irfft(X.real ** 2 + X.imag ** 2)[: self.p + 1]
        if r[0] < 1e-9:
            return self.ident.copy()
        r = r * self.lagwin
        r[0] = r[0] * (1.0 + 1e-4) + 1e-10
        try:
            sol = solve_toeplitz(r[: self.p], r[1: self.p + 1], check_finite=False)
        except Exception:
            return self.ident.copy()
        a = np.empty(self.p + 1)
        a[0] = 1.0
        a[1:] = -sol
        a *= self.gamma
        if not np.all(np.isfinite(a)):
            return self.ident.copy()
        return a

    def warp(self, a, beta):
        """Сдвиг формант в beta раз: огибающая 1/|A|² растягивается по оси
        частот, затем по ней строится новый устойчивый LPC-фильтр."""
        if abs(beta - 1.0) < 1e-6 or not np.any(a[1:]):
            return a
        try:
            A = np.fft.rfft(a, self.nw)
            Q = 1.0 / np.maximum(A.real ** 2 + A.imag ** 2, 1e-12)
            Qw = np.interp(self.widx / beta, self.widx, Q, right=Q[-1])
            r = np.fft.irfft(Qw)[: self.p + 1]
            r = r * self.wlag
            r[0] = r[0] * (1.0 + 1e-5) + 1e-12
            sol = solve_toeplitz(r[: self.p], r[1: self.p + 1], check_finite=False)
            out = np.empty(self.p + 1)
            out[0] = 1.0
            out[1:] = -sol
            out *= self.gamma
            if not np.all(np.isfinite(out)):
                return a
            return out
        except Exception:
            return a


def _ir_energy(a, n=256):
    imp = np.zeros(n)
    imp[0] = 1.0
    h = signal.lfilter([1.0], a, imp)
    return float(np.dot(h, h)) + 1e-12


# ═════════════════════════════════════════════════════════════════════════
#  Трекер основного тона (NSDF / McLeod, на децимированном сигнале)
# ═════════════════════════════════════════════════════════════════════════
class _Tracker:
    def __init__(self, sr, fmin, fmax):
        self.sr = sr
        self.R = max(1, int(round(sr / 6000.0)))
        self.sd = sr / float(self.R)
        self.minlag = max(2, int(self.sd / fmax))
        self.maxlag = int(math.ceil(self.sd / fmin)) + 1
        self.lagN = int(self.maxlag * 1.45) + 2
        self.W = int(self.maxlag * 2.6)
        self.nfft = 1 << int(math.ceil(math.log2(self.W * 2)))
        self.sos = signal.butter(4, 1100.0 / (sr / 2.0), output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2))
        self.buf = np.zeros(self.W)
        self.abs_next = 0
        self.centers = []
        self.Ts = []
        self.voiced_l = []
        self.voiced = False
        self.last_T = None
        self._arr = None

    def push(self, x, n_in):
        y, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        start = (-self.abs_next) % self.R
        xd = y[start:: self.R]
        self.abs_next += len(x)
        if len(xd) == 0:
            return
        self.buf = np.concatenate([self.buf, xd])[-self.W:]
        center = n_in - 1 - (self.W * self.R) // 2
        T, clar = self._estimate()
        thr = 0.45 if self.voiced else 0.60
        v = T is not None and clar >= thr
        self.voiced = v
        if v:
            self.last_T = T if self.last_T is None else 0.5 * self.last_T + 0.5 * T
            Tval = T
        else:
            Tval = self.last_T if self.last_T is not None else 0.0
        self.centers.append(center)
        self.Ts.append(Tval)
        self.voiced_l.append(v)
        if len(self.centers) > 48:
            self.centers = self.centers[-48:]
            self.Ts = self.Ts[-48:]
            self.voiced_l = self.voiced_l[-48:]
        self._arr = None

    def _estimate(self):
        f = self.buf - self.buf.mean()
        W = self.W
        if math.sqrt(float(np.dot(f, f)) / W) < 0.0015:
            return None, 0.0
        F = np.fft.rfft(f, self.nfft)
        r = np.fft.irfft(F.real ** 2 + F.imag ** 2)[: self.lagN]
        cs = np.concatenate([[0.0], np.cumsum(f * f)])
        lags = np.arange(self.lagN)
        m = cs[W - lags] + (cs[W] - cs[lags])
        nsdf = 2.0 * r / (m + 1e-12)
        neg = np.where(nsdf < 0)[0]
        if len(neg) == 0:
            return None, 0.0
        n = len(nsdf)
        i = int(neg[0])
        peaks = []
        while i < n - 1:
            while i < n and nsdf[i] <= 0:
                i += 1
            j = i
            while j < n and nsdf[j] > 0:
                j += 1
            if j > i and j < n:
                k = i + int(np.argmax(nsdf[i:j]))
                if self.minlag <= k <= self.maxlag and 1 <= k < n - 1:
                    peaks.append(k)
            i = j
        if not peaks:
            return None, 0.0
        vals = [nsdf[k] for k in peaks]
        vmax = max(vals)
        if vmax <= 0:
            return None, 0.0
        pick = next(k for k, v in zip(peaks, vals) if v >= 0.9 * vmax)
        if self.last_T is not None and self.voiced:
            lt = self.last_T / self.R
            if abs(pick / lt - 1.0) > 0.25:
                alt = [k for k, v in zip(peaks, vals)
                       if v >= 0.8 * vmax and abs(k / lt - 1.0) <= 0.2]
                if alt:
                    pick = min(alt, key=lambda k: abs(k - lt))
        y0, y1, y2 = nsdf[pick - 1], nsdf[pick], nsdf[pick + 1]
        den = y0 - 2.0 * y1 + y2
        d = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
        d = max(-1.0, min(1.0, d))
        lag = pick + d
        clar = y1 - 0.25 * (y0 - y2) * d
        return lag * self.R, float(clar)

    def at(self, t):
        """(озвучено?, период в сэмплах) в момент t (интерполяция по истории)."""
        if not self.centers:
            return False, None
        if self._arr is None:
            self._arr = (np.array(self.centers, dtype=np.float64),
                         np.array(self.Ts, dtype=np.float64),
                         np.array(self.voiced_l, dtype=bool))
        c, T, v = self._arr
        idx = int(np.argmin(np.abs(c - t)))
        voiced = bool(v[idx])
        if v.any():
            Tv = float(np.interp(t, c[v], T[v]))
        else:
            Tv = float(T[idx]) if T[idx] > 0 else None
        return voiced, Tv


# ═════════════════════════════════════════════════════════════════════════
#  Один голос PSOLA (своя высота), общий источник/анализ
# ═════════════════════════════════════════════════════════════════════════
class _Voice:
    def __init__(self, ratio, gain, auto=False):
        self.ratio = float(ratio)
        self.gain = float(gain)
        self.auto = bool(auto)
        self.s_next = None
        self.Rprev = None
        self.smooth = 1.0
        self.y = None
        self.w = None


# ═════════════════════════════════════════════════════════════════════════
#  Движок LP-PSOLA
# ═════════════════════════════════════════════════════════════════════════
class _Engine:
    def __init__(self, sr, spec):
        self.sr = int(sr)
        sc = self.sr / 48000.0
        self.mode = spec["mode"]
        self.beta = float(spec["formant"])

        # ── сегменты LPC: 5 мс шаг, 30 мс окно, порядок 20 ───────────
        self.SEG = max(64, int(round(240 * sc)))         # 5 мс
        self.P = 20 if self.sr >= 40000 else 16
        self.lpc = _Lpc(self.sr, self.P, int(round(1440 * sc)))  # 30 мс

        # ── диапазон основного тона ─────────────────────────────────
        self.fmin, self.fmax = 70.0, 450.0
        self.Tmin = int(self.sr / self.fmax)
        self.Tmax = int(self.sr / self.fmin)

        # ── задержка выдачи (запас на 2 периода для 70 Гц) ──────────
        self.D = int(round(2048 * sc))

        # ── сглаживание энергии остатка ────────────────────────────
        self.kw = max(8, int(round(0.0006 * self.sr)))
        self.kern = np.hanning(2 * self.kw + 3)[1:-1]
        self.kern /= self.kern.sum()

        keep = 16384
        self.xb = _Buf(keep)       # вход
        self.eb = _Buf(keep)       # остаток
        self.esb = _Buf(keep)      # сглаженная энергия остатка

        self.r_ptr = 0
        self.es_end = 0
        self.cur_k = -1
        self.segs = {}
        self.ys_hist = np.zeros(self.P)
        self.g_prev = 1.0
        self.agc = 1.0
        self.e_ptr = -self.D
        self.rng = np.random.default_rng(12345)

        self.pitch_mode = self.mode in ("psola", "autotune")
        self.tr = _Tracker(self.sr, self.fmin, self.fmax) if self.pitch_mode else None
        self.run = False
        self.scan_t = 0
        self.mk = []
        self.mkT = []

        self.voices = [_Voice(r, g, auto=(self.mode == "autotune"))
                       for (r, g) in spec["voices"]]
        for v in self.voices:
            v.y = _OLA(self.e_ptr)
            v.w = _OLA(self.e_ptr)
        self.ola_v = _OLA(self.e_ptr)
        self.stats = {"late": 0, "clipped": 0}

    # ── вход ─────────────────────────────────────────────────────────────
    def process(self, x):
        n = len(x)
        self.xb.append(x)
        n_in = self.xb.end
        if self.tr is not None:
            self.tr.push(x, n_in)
        self._residual(n_in)
        self._update_es(n_in)
        if self.pitch_mode:
            self._marks(n_in)
            self._synth(n_in, False)
        return self._emit(n, n_in)

    # ── LPC-остаток (обратный фильтр, кусочно-постоянные коэффициенты) ───
    def _new_segment(self, k):
        t0 = (k + 1) * self.SEG               # окно кончается в конце сегмента
        a = self.lpc.analyse(self.xb.get(t0 - self.lpc.wl, t0))
        aw = self.lpc.warp(a, self.beta)
        if aw is a:
            g = 1.0
        else:
            g = math.sqrt(_ir_energy(a) / _ir_energy(aw))
            g = min(4.0, max(0.25, g))
        self.segs[k] = (a, aw, g)
        # чистим старые сегменты
        old = k - (self.D // self.SEG) - 16
        for kk in [q for q in self.segs if q < old]:
            del self.segs[kk]

    def _residual(self, n_in):
        P = self.P
        t = self.r_ptr
        while t < n_in:
            k = t // self.SEG
            if k != self.cur_k:
                if (k + 1) * self.SEG > n_in:
                    break                        # ещё нет данных на окно
                self._new_segment(k)
                self.cur_k = k
            t_end = min(n_in, (k + 1) * self.SEG)
            a = self.segs[k][0]
            xs = self.xb.get(t - P, t_end)
            e = signal.lfilter(a, [1.0], xs)[P:]
            self.eb.append(e)
            t = t_end
        self.r_ptr = t

    def _update_es(self, n_in):
        kw = self.kw
        a = max(self.es_end - kw, self.eb.base + kw)
        lo = max(a - kw, self.eb.base)
        seg = self.eb.get(lo, n_in)
        if len(seg) == 0:
            return
        sm = np.convolve(seg * seg, self.kern, mode="same")
        self.esb.truncate(a)
        if self.esb.end < a:
            self.esb.append(np.zeros(a - self.esb.end))
        self.esb.append(sm[a - lo:])
        self.es_end = n_in

    # ── метки основного тона ─────────────────────────────────────────────
    def _clipT(self, T, fallback):
        if T is None or T <= 0:
            T = fallback
        return int(min(max(T, self.Tmin), self.Tmax))

    def _marks(self, n_in):
        guard = 2 * self.kw + 4
        step = max(16, self.SEG // 2)
        while True:
            if not self.run:
                t = self.scan_t
                v, T = self.tr.at(t)
                if not v:
                    if t + int(1.4 * self.Tmax) + guard > n_in:
                        break
                    self.scan_t = t + step
                    continue
                T = self._clipT(T, self.Tmax // 2)
                if t + int(1.25 * T) + guard > n_in:
                    break
                seg = self.esb.get(t, t + int(1.1 * T))
                m0 = t + int(np.argmax(seg))
                for vo in self.voices:
                    vo.s_next = float(m0)
                    vo.Rprev = None
                self.mk = [m0]
                self.mkT = [T]
                self.run = True
            else:
                m_prev, Tp = self.mk[-1], self.mkT[-1]
                v, Ttr = self.tr.at(m_prev + Tp)
                T = self._clipT(Tp, Tp)
                if len(self.mk) >= 2:
                    T = self._clipT(0.65 * Tp + 0.35 * (self.mk[-1] - self.mk[-2]), Tp)
                    if Ttr:
                        T = int(min(max(T, 0.75 * Ttr), 1.35 * Ttr))
                elif Ttr:
                    T = self._clipT(Ttr, Tp)
                exp = m_prev + T
                w = max(2, int(0.3 * T))
                if exp + w + guard > n_in:
                    break
                if not v:
                    self.run = False
                    self.scan_t = m_prev + int(0.7 * T)
                    continue
                h = max(4, T // 2)
                if exp + w + h + guard > n_in:
                    break
                seg = self.esb.get(exp - w, exp + w + 1)
                if len(seg) == 0 or float(seg.max()) < 1e-12:
                    self.run = False
                    self.scan_t = m_prev + int(0.7 * T)
                    continue
                m = self._track_mark(m_prev, exp, w, h, seg)
                self.mk.append(m)
                self.mkT.append(self._clipT(m - m_prev, T))
                if len(self.mk) > 96:
                    self.mk = self.mk[-96:]
                    self.mkT = self.mkT[-96:]

    def _track_mark(self, m_prev, exp, w, h, es_seg):
        """Следующая метка: сдвиг предыдущего периода по max нормированной
        кросс-корреляции ФОРМЫ ОСТАТКА (pitch-synchronous, без «дрожания»).
        Именно на остатке — метки не зависят от формант."""
        ref = self.eb.get(m_prev - h, m_prev + h + 1)
        cand = self.eb.get(exp - w - h, exp + w + h + 1)
        if len(ref) < 2 * h + 1 or len(cand) < 2 * h + 1:
            return exp
        num = np.correlate(cand, ref, mode="valid")
        cs = np.concatenate([[0.0], np.cumsum(cand * cand)])
        win = 2 * h + 1
        ew = cs[win:] - cs[:-win]
        er = float(np.dot(ref, ref))
        ncc = num / np.sqrt(ew * er + 1e-12)
        u = (np.arange(len(ncc)) - w) / float(w)
        score = ncc * (1.0 - 0.25 * u * u)
        k = int(np.argmax(score))
        if ncc[k] < 0.10:
            k = int(np.argmax(es_seg * (1.0 - 0.4 * u * u)))
        elif 0 < k < len(ncc) - 1:
            y0, y1, y2 = score[k - 1], score[k], score[k + 1]
            den = y0 - 2.0 * y1 + y2
            if abs(den) > 1e-12:
                k += int(round(max(-1.0, min(1.0, 0.5 * (y0 - y2) / den))))
        return exp - w + k

    # ── синтез: перестановка импульсов с новым шагом ─────────────────────
    def _synth(self, n_in, flush):
        if not self.mk:
            return
        for vi, vo in enumerate(self.voices):
            self._synth_voice(vi, vo, n_in, flush)

    def _synth_voice(self, vi, vo, n_in, flush):
        mk = np.array(self.mk, dtype=np.float64)
        mkT = np.array(self.mkT, dtype=np.float64)
        while vo.s_next is not None:
            sf = vo.s_next                      # дробная позиция импульса
            if sf > mk[-1] + 0.5 * mkT[-1]:
                if self.run and not flush:
                    break
                vo.s_next = None
                vo.Rprev = None
                break
            i = int(np.argmin(np.abs(mk - sf)))
            m = int(mk[i])
            Tg = int(round(mkT[i]))
            if not flush and m + Tg > n_in:
                break
            # локальный период по наклону прямой через соседние метки
            span = 4 if vo.auto else 3
            lo, hi = max(0, i - span), min(len(mk) - 1, i + span)
            if hi - lo >= 2:
                jj = np.arange(lo, hi + 1, dtype=np.float64)
                jj -= jj.mean()
                Tloc = float(np.dot(jj, mk[lo:hi + 1] - mk[lo:hi + 1].mean())
                             / np.dot(jj, jj))
            elif hi > lo:
                Tloc = (mk[hi] - mk[lo]) / (hi - lo)
            else:
                Tloc = float(mkT[i])
            Tloc = min(max(Tloc, self.Tmin), self.Tmax)

            if vo.auto:
                # автотюн: тянем к ближайшей полутоновой ступени (в MIDI)
                f0 = self.sr / Tloc
                midi = 69.0 + 12.0 * math.log2(f0 / 440.0)
                tgt = 2.0 ** ((round(midi) - midi) / 12.0)
                tgt = min(1.19, max(0.84, tgt))
                vo.smooth += 0.85 * (tgt - vo.smooth)
                alpha = vo.smooth
            else:
                alpha = vo.ratio
            Tsf = Tloc / alpha
            Tsf = max(self.sr / 1000.0, min(Tsf, 3.0 * self.Tmax))
            s = int(round(sf))
            nxt = sf + Tsf
            Ts = max(1, int(round(nxt)) - s)
            win = _hann(Tg)
            grain = self.eb.get(m - Tg, m + Tg + 1) * win
            if s - Tg < vo.y.base:
                self.stats["late"] += 1
            vo.y.add(s - Tg, grain)
            vo.w.add(s - Tg, win)
            if vi == 0:
                L = vo.Rprev if vo.Rprev is not None else Ts
                if s - L < self.ola_v.base:
                    self.stats["clipped"] += 1
                    L = max(0, s - self.ola_v.base)
                if L > 0:
                    self.ola_v.add(s - L, _ramp(L))
                self.ola_v.add(s, 1.0 - _ramp(Ts))
            vo.Rprev = Ts
            vo.s_next = nxt

    # ── выдача готовых сэмплов ───────────────────────────────────────────
    def _emit(self, n, n_in):
        t0 = self.e_ptr
        t1 = t0 + n
        out = np.zeros(n)
        a0 = max(t0, 0)
        if t1 > a0:
            off = a0 - t0
            Vfull = self.ola_v.take(n)
            V = Vfull[off:]
            ys_all = []
            for vo in self.voices:
                Y = vo.y.take(n)[off:]
                W = vo.w.take(n)[off:]
                # защита от деления на почти-ноль
                ys_all.append(vo.gain * Y / np.maximum(W, 1e-2))
            dry = self._dry_excitation(a0, t1)
            if ys_all:
                Ytot = ys_all[0]
                for q in ys_all[1:]:
                    Ytot = Ytot + q
                V = np.clip(V, 0.0, 1.0)
                e_mix = V * Ytot + (1.0 - V) * dry
            else:
                e_mix = dry
            wet = self._synthesis(a0, t1, e_mix)
            ref = self.xb.get(a0, t1)
            wet = self._agc(wet, ref)
            out[off:] = wet
        else:
            self.ola_v.take(n)
            for vo in self.voices:
                vo.y.take(n)
                vo.w.take(n)
        self.e_ptr = t1
        return out

    def _dry_excitation(self, a0, t1):
        if self.mode != "whisper":
            return self.eb.get(a0, t1).copy()
        pad = self.kw * 4
        seg = self.eb.get(a0 - pad, t1 + pad)
        kern = np.hanning(2 * (self.sr // 250) + 3)[1:-1]
        kern /= kern.sum()
        env = np.sqrt(np.maximum(np.convolve(seg * seg, kern, mode="same"), 0.0))
        env = env[pad:pad + (t1 - a0)]
        return self.rng.standard_normal(t1 - a0) * env

    def _lpc_state(self, a, yh):
        """Начальное состояние для lfilter([1], a, x, zi=zi) такое, чтобы
        фильтр продолжился с прошлых выходов yh = [y[-P], ..., y[-1]]."""
        P = self.P
        zi = np.zeros(P)
        for m in range(P):
            s = 0.0
            base = m + P
            for k in range(m + 1, P + 1):
                s += a[k] * yh[base - k]
            zi[m] = -s
        return zi

    def _synthesis(self, a0, t1, e_mix):
        out = np.empty(t1 - a0)
        t = a0
        P = self.P
        while t < t1:
            k = t // self.SEG
            seg = self.segs.get(k)
            if seg is None:
                self._new_segment(k)
                seg = self.segs[k]
            _, aw, g = seg
            tb = min(t1, (k + 1) * self.SEG)
            eb = e_mix[t - a0:tb - a0]
            zi = self._lpc_state(aw, self.ys_hist)
            yb, _ = signal.lfilter([1.0], aw, eb, zi=zi)
            if not np.all(np.isfinite(yb)):
                yb = np.zeros_like(eb)
                self.ys_hist[:] = 0.0
            self.ys_hist = np.concatenate([self.ys_hist, yb])[-P:]
            if g != 1.0 or self.g_prev != 1.0:
                yb = yb * np.linspace(self.g_prev, g, len(yb) + 1)[1:]
            self.g_prev = g
            out[t - a0:tb - a0] = yb
            t = tb
        return out

    def _agc(self, wet, ref):
        rd = math.sqrt(float(np.mean(ref * ref)) + 1e-12)
        rw = math.sqrt(float(np.mean(wet * wet)) + 1e-12)
        if rd > 3e-4 and rw > 1e-6:
            tgt = min(3.0, max(0.33, rd / rw))
        else:
            tgt = self.agc
        new = self.agc + 0.25 * (tgt - self.agc)
        g = np.linspace(self.agc, new, len(wet) + 1)[1:]
        self.agc = new
        return wet * g


# ═════════════════════════════════════════════════════════════════════════
#  Эффекты после движка (эхо, насыщение, тембр)
# ═════════════════════════════════════════════════════════════════════════
class _Delay:
    def __init__(self, delay_samples, mix):
        self.delay = max(1, int(delay_samples))
        self.mix = float(mix)
        self.hist = np.zeros(self.delay)

    def process(self, x):
        n = len(x)
        ext = np.concatenate([self.hist, x])
        delayed = ext[:n]
        self.hist = ext[n:n + self.delay] if len(ext) >= n + self.delay else ext[-self.delay:]
        return x * (1.0 - 0.5 * self.mix) + delayed * self.mix


class _Sos:
    def __init__(self, sos):
        self.sos = sos
        self.zi = signal.sosfilt_zi(sos) * 0.0

    def process(self, x):
        y, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        return y


def _lowshelf(sr, f0, gain_db, q=0.707):
    A = 10 ** (gain_db / 40.0)
    w = 2 * math.pi * f0 / sr
    cw, sw = math.cos(w), math.sin(w)
    al = sw / (2 * q)
    sA = 2 * math.sqrt(A) * al
    b0 = A * ((A + 1) - (A - 1) * cw + sA)
    b1 = 2 * A * ((A - 1) - (A + 1) * cw)
    b2 = A * ((A + 1) - (A - 1) * cw - sA)
    a0 = (A + 1) + (A - 1) * cw + sA
    a1 = -2 * ((A - 1) + (A + 1) * cw)
    a2 = (A + 1) + (A - 1) * cw - sA
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _saturate(x, drive):
    return np.tanh(x * drive) / math.tanh(drive)


class _PostChain:
    """Лёгкая обработка «характера» поверх чистого движка."""

    def __init__(self, fid, sr):
        self.fid = fid
        self.sr = sr
        self.rng = np.random.default_rng(777)
        if fid == FILTER_VADER:
            self.shelf = _Sos(_lowshelf(sr, 180.0, 4.0))
            self.delay = _Delay(int(sr * 0.016), 0.20)
            self.bp = _Sos(signal.butter(2, [900 / (sr / 2), 4500 / (sr / 2)],
                                         btype="band", output="sos"))
            self.env = 0.0
        elif fid == FILTER_DEMON:
            self.shelf = _Sos(_lowshelf(sr, 140.0, 5.0))
            self.delay = _Delay(int(sr * 0.052), 0.22)
            self.delay2 = _Delay(int(sr * 0.117), 0.10)
            self.lp = _Sos(signal.butter(2, 7000 / (sr / 2), output="sos"))
        elif fid == FILTER_WHISPER:
            self.lp = _Sos(signal.butter(2, 9000 / (sr / 2), output="sos"))

    def process(self, y):
        f = self.fid
        if f == FILTER_VADER:
            y = self.shelf.process(y)
            y = _saturate(y, 1.25)
            e = np.abs(y)
            self.env = 0.9 * self.env + 0.1 * float(np.mean(e))
            breath = self.bp.process(self.rng.standard_normal(len(y))) * (0.05 * self.env * 4.0)
            y = self.delay.process(y) + breath
        elif f == FILTER_DEMON:
            y = self.shelf.process(y)
            y = _saturate(y, 1.7)
            y = self.lp.process(y)
            y = self.delay.process(y)
            y = self.delay2.process(y)
        elif f == FILTER_WHISPER:
            y = self.lp.process(y) * 0.9
        return y


# ═════════════════════════════════════════════════════════════════════════
#  Публичный класс (интерфейс совместим со старой версией)
# ═════════════════════════════════════════════════════════════════════════
_warmed = False


def _warmup():
    """Один раз прогревает ленивые пути numpy/scipy, чтобы первый реальный
    чанк не вызвал скачок задержки в аудио-потоке."""
    global _warmed
    if _warmed:
        return
    _warmed = True
    try:
        rng = np.random.default_rng(1)
        for sp_ in (_SPECS[FILTER_VADER], _SPECS[FILTER_WHISPER]):
            eng = _Engine(SAMPLE_RATE, sp_)
            for _ in range(8):
                eng.process(rng.standard_normal(960) * 0.05)
    except Exception:
        pass


class VoiceFilterBank:
    """Держит состояние фильтров и применяет выбранный к чанку."""

    def __init__(self, sample_rate=SAMPLE_RATE):
        self.sr = int(sample_rate)
        self._req = FILTER_NONE
        self._dirty = False
        self._eng = None
        self._post = None
        self._fail = 0

    @property
    def active(self):
        return self._req

    def set_filter(self, fid):
        if fid not in FILTER_LABELS:
            fid = FILTER_NONE
        if fid != self._req:
            self._req = fid
            self._dirty = True

    def _build(self):
        _warmup()
        fid = self._req
        self._eng = None
        self._post = None
        if fid in _SPECS:
            self._eng = _Engine(self.sr, _SPECS[fid])
            self._post = _PostChain(fid, self.sr)
        self._dirty = False

    def process(self, x):
        fid = self._req
        if fid == FILTER_NONE or fid not in _SPECS:
            return x
        if self._dirty or self._eng is None:
            self._build()
        n = len(x)
        if n == 0:
            return x
        try:
            xa = np.asarray(x, dtype=np.float64)
            if not np.all(np.isfinite(xa)):
                xa = np.nan_to_num(xa, nan=0.0, posinf=0.0, neginf=0.0)
            y = self._eng.process(xa)
            y = self._post.process(y)
            if not np.all(np.isfinite(y)):
                raise FloatingPointError("non-finite output")
            peak = float(np.max(np.abs(y))) if n else 0.0
            if peak > 0.98:
                y = np.tanh(y * (0.98 / peak)) * 0.98
            self._fail = 0
            return y.astype(np.float32)
        except Exception as e:
            self._fail += 1
            if self._fail <= 3:
                print(f"[VoiceFilter] error in '{fid}': {e}", flush=True)
            self._dirty = True
            return x