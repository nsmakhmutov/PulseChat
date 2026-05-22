import asyncio
import threading
import queue
import math
from collections import deque
from typing import Callable
import numpy as np
import sounddevice as sd
import opuslib
import heapq
import struct
import time
import ctypes
import os
import av
from aiortc import AudioStreamTrack
from PyQt6.QtCore import QObject, pyqtSignal, QSettings
from config import (
    SAMPLE_RATE, CHANNELS, CHUNK_SIZE, FRAME_DURATION,
    OPUS_APPLICATION, DEFAULT_BITRATE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE,
    FLAG_STREAM_VOICES, FLAG_WHISPER, FLAG_ANONYMOUS,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    ANONYMOUS_UID,
    AUDIO_DIAG_ENABLED,
)
from .audio_processing import (
    DeepFilterEngine,
    PYRNNOISE_AVAILABLE, PYAUDIOWPATCH_AVAILABLE, _pyaudio,
    butter, sosfilt, sosfilt_zi, _WLP_SOS, _ANON_LP_SOS,
)
from .audio_capture import StreamAudioCapture, MicrophoneTrack, SystemAudioTrack, get_dll
try:
    from pyrnnoise import RNNoise
except ImportError:
    RNNoise = None


class JitterBuffer:
    SEQ_JUMP_RESET_BACK    = 200
    SEQ_JUMP_RESET_FORWARD = 10000

    def __init__(self, target_delay=2):
        self.buffer = []
        self.target_delay = target_delay
        self.last_seq = -1
        self._lock = threading.Lock()
        self.is_buffering = True
        self.max_size = 50

    def _reset_locked(self):
        self.buffer.clear()
        self.last_seq = -1
        self.is_buffering = True

    def reset(self):
        with self._lock:
            self._reset_locked()

    def add(self, seq, data):
        with self._lock:
            if self.last_seq != -1:
                if seq + self.SEQ_JUMP_RESET_BACK < self.last_seq:
                    print(f"[JB] seq reset (was last={self.last_seq}, got={seq}) — "
                          f"смена сессии, сброс буфера", flush=True)
                    self._reset_locked()
                elif seq > self.last_seq + self.SEQ_JUMP_RESET_FORWARD:
                    print(f"[JB] seq jump forward (last={self.last_seq}, got={seq}) — "
                          f"reset", flush=True)
                    self._reset_locked()

            if seq <= self.last_seq and self.last_seq != -1:
                return
            heapq.heappush(self.buffer, (seq, data))
            if len(self.buffer) > self.max_size:
                max_idx = 0
                max_seq = self.buffer[0][0]
                for i in range(1, len(self.buffer)):
                    if self.buffer[i][0] > max_seq:
                        max_seq = self.buffer[i][0]
                        max_idx = i
                # Swap с последним и pop — избегает сдвига на O(n)
                last_idx = len(self.buffer) - 1
                if max_idx != last_idx:
                    self.buffer[max_idx] = self.buffer[last_idx]
                self.buffer.pop()
                if max_idx != last_idx:
                    heapq.heapify(self.buffer)

    def get(self):
        with self._lock:
            if not self.buffer:
                self.is_buffering = True
                return None

            if self.is_buffering:
                if len(self.buffer) >= self.target_delay:
                    self.is_buffering = False
                else:
                    return None

            seq, data = heapq.heappop(self.buffer)
            self.last_seq = seq
            return data



class RemoteUser:
    def __init__(self, uid):
        self.uid = uid
        self.jitter_buffer = JitterBuffer()
        self.decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
        self.last_packet_time = 0
        self.volume = 1.0
        self.is_locally_muted = False
        self.volume_zero = False
        self.remote_muted = False
        self.remote_deafened = False


class AudioHandler(QObject):
    volume_level_signal = pyqtSignal(int)
    status_changed = pyqtSignal(bool, bool)
    whisper_received = pyqtSignal(int)
    whisper_ended = pyqtSignal()

    user_volume_zero = pyqtSignal(int, bool)

    def _apply_anonymous_voice_effect(self, s: np.ndarray, state: dict) -> np.ndarray:

        N = len(s)
        max_delay = 1440
        speed = 2.0 ** (-4.0 / 12.0)
        rate = 1.0 - speed

        H = 2048
        history = state['history']
        buf     = state['buf']
        buf[:H]      = history
        buf[H:H + N] = s
        buf_view = buf[:H + N]

        phases = state['phase'] + state['_arange'] * rate / max_delay
        state['phase'] = float((phases[-1] + rate / max_delay) % 1.0)

        p1 = phases % 1.0
        p2 = (phases + 0.5) % 1.0

        d1 = p1 * max_delay
        d2 = p2 * max_delay

        base_idx = state['_base']
        r1 = base_idx - d1
        r2 = base_idx - d2

        i1_floor = np.floor(r1).astype(np.int32)
        i2_floor = np.floor(r2).astype(np.int32)

        buf_last = H + N - 1
        i1_ceil = np.clip(i1_floor + 1, 0, buf_last)
        i2_ceil = np.clip(i2_floor + 1, 0, buf_last)

        frac_1 = r1 - i1_floor
        frac_2 = r2 - i2_floor

        val_1 = buf_view[i1_floor] * (1.0 - frac_1) + buf_view[i1_ceil] * frac_1
        val_2 = buf_view[i2_floor] * (1.0 - frac_2) + buf_view[i2_ceil] * frac_2

        fade_1 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p1)
        fade_2 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p2)

        shifted = val_1 * fade_1 + val_2 * fade_2

        history[:] = buf_view[N:]

        out, state['lp_zi'] = sosfilt(self._anon_lp_sos, shifted, zi=state['lp_zi'])

        peak = np.max(np.abs(out))
        if peak > 0.9:
            out *= 0.9 / peak

        return out.astype(np.float32)

    def __init__(self):
        super().__init__()

        self.settings = QSettings("MyVoiceChat", "UserVolumes")
        self.global_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.uid_to_ip = {}
        self.pending_volumes = {}
        self.remote_users = {}
        self.users_lock = threading.Lock()

        self.encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION)

        saved_bitrate = int(self.global_settings.value("audio_bitrate", DEFAULT_BITRATE))
        self.encoder.bitrate = saved_bitrate
        self.encoder.complexity = 5

        self.denoiser = None
        self.dfn_engine = None
        self.dfn_available = False

        self.nr_mode = int(self.global_settings.value("audio/nr_mode", 1))

        try:
            self.dfn_engine = DeepFilterEngine()
            self.dfn_available = True
            print("[Audio] DeepFilterNet3 инициализирован успешно.")
        except Exception as e:
            self.dfn_available = False
            print(f"[Audio] DFN не загружен: {e}")

        if PYRNNOISE_AVAILABLE:
            try:
                self.denoiser = RNNoise(sample_rate=SAMPLE_RATE)
                print("[Audio] RNNoise инициализирован.")
            except Exception as e:
                print(f"[Audio] Ошибка RNNoise: {e}")

        if self.nr_mode == 1 and not PYRNNOISE_AVAILABLE:
            print("[Audio] RNNoise недоступен, шумоподавление отключено.")
            self.nr_mode = 0
        elif self.nr_mode == 2 and not self.dfn_available:
            print("[Audio] DeepFilterNet недоступен, откат на RNNoise.")
            self.nr_mode = 1 if PYRNNOISE_AVAILABLE else 0

        self.incoming_packets = queue.Queue(maxsize=500)
        self.send_queue = queue.Queue(maxsize=100)
        self._local_sounds: list = []
        self._local_sounds_lock = threading.Lock()
        _STREAM_BUF_CHUNKS = 60
        self._STREAM_BUF_SIZE: int  = CHUNK_SIZE * _STREAM_BUF_CHUNKS
        self._stream_buf:  np.ndarray = np.zeros(self._STREAM_BUF_SIZE + CHUNK_SIZE,
                                                  dtype=np.float32)
        self._stream_fill: int = 0
        self._stream_lock  = threading.Lock()
        self._stream_vol:   float = 1.0
        self._stream_active: bool = False
        self._is_running = threading.Event()
        self._is_muted = threading.Event()
        self._is_deafened = threading.Event()
        self.mix_buffer = np.zeros(CHUNK_SIZE, dtype=np.float32)
        self._pcm_int16_buf = np.zeros(CHUNK_SIZE, dtype=np.int16)
        self._cb_first_logged: bool = False
        self._vol_emit_counter: int = 0
        self.saved_vad_slider = int(self.global_settings.value("vad_threshold_slider", 5))
        self.vad_threshold = self.saved_vad_slider / 1000.0
        self.vad_hangover = 0.4
        self.last_voice_time = 0
        self.my_uid = 0
        self.my_sequence = 0
        self.whisper_target_uid: int = 0
        self._whisper_sequence: int = 0
        self._whisper_anonymous: bool = False
        self._anon_last_incoming_ts: float = 0.0
        self._wlp_sos = _WLP_SOS
        self._wlp_zi  = sosfilt_zi(self._wlp_sos).astype(np.float64)
        self._anon_lp_sos = _ANON_LP_SOS
        self._whisper_states: dict = {}
        self._active_whispers: dict = {}
        self._whisper_in_uid: int = 0
        self._whisper_in_ts: float = 0.0
        self._whisper_effect_reset: bool = False
        self.vad_pre_buffer = deque(maxlen=5)
        self.was_talking = False

        self.stream = None
        self._in_stream = None
        self._out_channels: int = 2
        self._in_channels:  int = 1

        self._in_sr:        int = SAMPLE_RATE
        self._out_sr:       int = SAMPLE_RATE
        self._in_blocksize: int = CHUNK_SIZE
        self._out_blocksize: int = CHUNK_SIZE
        self._out_resamp_buf: np.ndarray = np.zeros(CHUNK_SIZE, dtype=np.float32)
        self._cb_out_first_logged: bool = False

        self._mix_buffer_ptr: ctypes.POINTER(ctypes.c_float) = (
            self.mix_buffer.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        )

        self._diag_voice_sum:  float = 0.0
        self._diag_stream_sum: float = 0.0
        self._diag_mix_sum:    float = 0.0
        self._diag_cnt:        int   = 0
        self._diag_next_ts:    float = 0.0

        self._pkt_thread: threading.Thread | None = None

        self._audio_users_snapshot: dict = {}

    def enable_dll_render(self, active: bool, raw_mode: bool = False) -> None:

        pass

    def set_bitrate(self, bitrate_kbps):
        bitrate_bps = int(bitrate_kbps) * 1000
        try:
            with self.users_lock:
                if hasattr(self, 'encoder'):
                    self.encoder.bitrate = bitrate_bps
                    self.global_settings.setValue("audio_bitrate", bitrate_bps)
                    print(f"[Audio] Bitrate changed to {bitrate_kbps} kbps")
        except Exception as e:
            print(f"[Audio] Error setting bitrate: {e}")

    def play_internal_sound(self, data: np.ndarray, sr: int, vol: float = 1.0):
        try:
            data = np.asarray(data, dtype=np.float32)
            # Стерео → моно
            if data.ndim > 1 and data.shape[1] > 1:
                data = np.mean(data, axis=1)
            elif data.ndim > 1:
                data = data[:, 0]
            if sr != SAMPLE_RATE:
                target_len = int(round(len(data) * SAMPLE_RATE / sr))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, len(data), dtype=np.float32)
                    x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float32)
                    data = np.interp(x_new, x_old, data).astype(np.float32)

            with self._local_sounds_lock:
                self._local_sounds.append({'data': data, 'pos': 0, 'vol': float(vol)})
        except Exception as e:
            print(f"[Audio] play_internal_sound error: {e}")

    def add_stream_audio(self, data: np.ndarray, sr: int, vol: float = 1.0) -> None:

        try:
            data = np.asarray(data, dtype=np.float32)
            if data.ndim == 2:
                rows, cols = data.shape
                if rows == 1:
                    if cols % 2 == 0 and cols >= CHUNK_SIZE * 2:
                        mono = data[0].reshape(-1, 2).mean(axis=1).astype(np.float32)
                    else:
                        mono = data[0]
                elif rows > 2 and cols <= 2:
                    mono = np.mean(data, axis=1).astype(np.float32)
                else:
                    mono = np.mean(data, axis=0).astype(np.float32)
            elif data.ndim == 1:
                mono = data
            else:
                return

            if sr != SAMPLE_RATE:
                target_len = int(round(len(mono) * SAMPLE_RATE / sr))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, len(mono), dtype=np.float64)
                    x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float64)
                    mono  = np.interp(x_new, x_old, mono).astype(np.float32)

            if vol != 1.0:
                mono = mono * vol

            n = len(mono)
            with self._stream_lock:
                if self._stream_fill + n > self._STREAM_BUF_SIZE:
                    drop = self._stream_fill + n - self._STREAM_BUF_SIZE
                    remain = self._stream_fill - drop
                    if remain > 0:
                        self._stream_buf[:remain] = self._stream_buf[drop:self._stream_fill]
                    self._stream_fill = remain

                self._stream_buf[self._stream_fill:self._stream_fill + n] = mono
                self._stream_fill += n
                self._stream_active = True

        except Exception as e:
            print(f"[Audio] add_stream_audio error: {e}")

    def stop_stream_playback(self) -> None:
        self._stream_active = False
        with self._stream_lock:
            self._stream_fill = 0
            self._stream_buf[:] = 0.0

    def set_stream_volume(self, vol: float) -> None:
        self._stream_vol = max(0.0, min(float(vol), 2.0))

    def set_nr_mode(self, mode: int):
        self.nr_mode = int(mode)
        self.global_settings.setValue("audio/nr_mode", self.nr_mode)
        labels = {0: "выкл", 1: "RNNoise", 2: "DeepFilterNet"}
        print(f"[Audio] NR mode → {labels.get(self.nr_mode, "?")} ({self.nr_mode})")

    def set_vad_threshold(self, slider_val: int):
        threshold = max(1, min(50, slider_val)) / 1000.0
        self.vad_threshold = threshold
        self.global_settings.setValue("vad_threshold_slider", slider_val)
        print(f"[Audio] VAD threshold set to {threshold:.4f} (slider={slider_val})")

    def find_device_index_by_name(self, name, is_input=True):
        direction = "INPUT" if is_input else "OUTPUT"
        if not name:
            print(f"[DEBUG] find_device_index_by_name: {direction} name=None → вернём None (дефолт системы)", flush=True)
            return None
        devices = sd.query_devices()
        print(f"[DEBUG] find_device_index_by_name: ищем {direction} '{name}'", flush=True)
        for i, d in enumerate(devices):
            try:
                api_name = sd.query_hostapis(d['hostapi'])['name']
                full_name = f"{d['name']} ({api_name})"
                if full_name.strip() == name.strip():
                    if is_input and d['max_input_channels'] > 0:
                        print(f"[DEBUG] find_device_index_by_name: найдено {direction} idx={i} '{full_name}'", flush=True)
                        return i
                    if not is_input and d['max_output_channels'] > 0:
                        print(f"[DEBUG] find_device_index_by_name: найдено {direction} idx={i} '{full_name}'", flush=True)
                        return i
            except:
                continue
        print(f"[DEBUG] find_device_index_by_name: {direction} '{name}' НЕ НАЙДЕНО → None (дефолт)", flush=True)
        return None

    def start(self, input_name=None, output_name=None):
        print(f"[DEBUG] AudioHandler.start: BEGIN — input_name={input_name!r}, output_name={output_name!r}", flush=True)
        if self.my_uid == 0:
            print("[DEBUG] AudioHandler.start: my_uid==0, выход", flush=True)
            return
        print("[DEBUG] AudioHandler.start: вызов stop()...", flush=True)
        self.stop()
        time.sleep(0.1)
        print("[DEBUG] AudioHandler.start: stop() выполнен", flush=True)

        with self.users_lock:
            for user in self.remote_users.values():
                user.decoder       = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
                user.jitter_buffer = JitterBuffer()
            self._audio_users_snapshot = dict(self.remote_users)

        print("[DEBUG] AudioHandler.start: поиск устройств...", flush=True)
        in_idx = self.find_device_index_by_name(input_name, True)
        out_idx = self.find_device_index_by_name(output_name, False)
        out_idx = self._remap_to_wasapi(out_idx)

        try:
            _in_dev_info  = sd.query_devices(in_idx  if in_idx  is not None else sd.default.device[0])
            self._in_sr   = int(_in_dev_info['default_samplerate'])
        except Exception:
            self._in_sr   = SAMPLE_RATE

        try:
            _out_dev_info  = sd.query_devices(out_idx if out_idx is not None else sd.default.device[1])
            self._out_sr   = int(_out_dev_info['default_samplerate'])
        except Exception:
            self._out_sr   = SAMPLE_RATE

        self._in_blocksize  = int(round(self._in_sr  * FRAME_DURATION / 1000))
        self._out_blocksize = int(round(self._out_sr * FRAME_DURATION / 1000))
        self._out_resamp_buf = np.zeros(self._out_blocksize, dtype=np.float32)

        if self._in_sr != SAMPLE_RATE:
            print(
                f"[Audio] ⚠ Микрофон: {self._in_sr} Гц ≠ 48000 — "
                f"автоматический ресемплинг {self._in_sr}→48000 Гц",
                flush=True,
            )
        if self._out_sr != SAMPLE_RATE:
            print(
                f"[Audio] ⚠ Динамики: {self._out_sr} Гц ≠ 48000 — "
                f"автоматический ресемплинг 48000→{self._out_sr} Гц",
                flush=True,
            )

        print(f"[DEBUG] AudioHandler.start: in_idx={in_idx}, out_idx={out_idx}", flush=True)

        self._is_running.set()
        try:
            print("[DEBUG] AudioHandler.start: создание sd.InputStream (микрофон)...", flush=True)
            self._in_stream = sd.InputStream(
                device=in_idx,
                samplerate=self._in_sr,
                blocksize=self._in_blocksize,
                dtype='float32', channels=CHANNELS,
                callback=self._input_callback,
            )
            print("[DEBUG] AudioHandler.start: создание sd.OutputStream (динамики WASAPI)...", flush=True)
            self.stream = sd.OutputStream(
                device=out_idx,
                samplerate=self._out_sr,
                blocksize=self._out_blocksize,
                dtype='float32', channels=CHANNELS,
                callback=self._output_callback,
            )
            self._in_stream.start()
            self.stream.start()
            print("[DEBUG] AudioHandler.start: InputStream + OutputStream запущены", flush=True)
            self._pkt_thread = threading.Thread(target=self._packet_processor_loop, daemon=True)
            self._pkt_thread.start()
            print("[DEBUG] AudioHandler.start: рабочий поток запущен — DONE", flush=True)
        except Exception as e:
            import traceback
            print(f"[DEBUG] AudioHandler.start: EXCEPTION:\n{traceback.format_exc()}", flush=True)
            self._is_running.clear()

    def stop(self):
        self._is_running.clear()

        t = getattr(self, '_pkt_thread', None)
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._pkt_thread = None
        if getattr(self, '_in_stream', None) is not None:
            try:
                self._in_stream.stop()
                self._in_stream.close()
            except Exception:
                pass
            self._in_stream = None
        if hasattr(self, 'stream') and self.stream:
            try:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            except Exception:
                pass

    def cleanup_users(self, active_uids):
        with self.users_lock:
            for uid in list(self.remote_users.keys()):
                if uid not in active_uids:
                    del self.remote_users[uid]

            for uid in list(self.uid_to_ip.keys()):
                if uid not in active_uids:
                    del self.uid_to_ip[uid]
            for uid in list(self.pending_volumes.keys()):
                if uid not in active_uids:
                    del self.pending_volumes[uid]

            for uid in list(self._whisper_states.keys()):
                if uid not in active_uids:
                    del self._whisper_states[uid]
            for uid in list(self._active_whispers.keys()):
                if uid not in active_uids:
                    del self._active_whispers[uid]
            self._audio_users_snapshot = dict(self.remote_users)

    def reset_voice_state(self):
        import queue as _q
        drained = 0
        try:
            while True:
                self.incoming_packets.get_nowait()
                drained += 1
        except _q.Empty:
            pass

        with self.users_lock:
            n_users = len(self.remote_users)
            self.remote_users.clear()
            self.uid_to_ip.clear()
            self.pending_volumes.clear()

            if hasattr(self, '_whisper_states'):
                self._whisper_states.clear()
            if hasattr(self, '_active_whispers'):
                self._active_whispers.clear()

            self._audio_users_snapshot = {}

        print(f"[Audio] reset_voice_state: drained={drained} пакетов, "
              f"удалено {n_users} RemoteUser", flush=True)

    def _packet_processor_loop(self):
        while self._is_running.is_set():
            try:
                packet_data = self.incoming_packets.get(timeout=0.1)
                uid, seq, data, flags = packet_data
                if uid == self.my_uid: continue

                with self.users_lock:
                    is_new_user = uid not in self.remote_users
                    if is_new_user:
                        self.remote_users[uid] = RemoteUser(uid)
                        if uid in self.pending_volumes:
                            val = self.pending_volumes.pop(uid)
                        else:
                            val = 1.0
                        val = float(val)
                        self.remote_users[uid].volume = val
                        self.remote_users[uid].volume_zero = (val == 0.0)

                    user = self.remote_users[uid]
                    user.remote_muted = bool(flags & 1)
                    user.remote_deafened = bool(flags & 2)

                    if data:
                        user.jitter_buffer.add(seq, data)
                        user.last_packet_time = time.perf_counter()

                    if is_new_user:
                        self._audio_users_snapshot = dict(self.remote_users)

            except queue.Empty:
                continue
            except Exception:
                pass

    def _input_callback(self, indata, frames, time_info, status):
        if not self._cb_first_logged:
            self._cb_first_logged = True
            print("[DEBUG] _input_callback: ПЕРВЫЙ ВЫЗОВ — InputStream работает", flush=True)
        if status:
            print(f"[DEBUG] _input_callback: status={status}", flush=True)

        if not self._is_running.is_set():
            return

        curr_time = time.perf_counter()
        raw_input = indata.flatten()

        if self._in_sr != SAMPLE_RATE:
            x_old = np.linspace(0.0, 1.0, len(raw_input), dtype=np.float64)
            x_new = np.linspace(0.0, 1.0, CHUNK_SIZE,     dtype=np.float64)
            raw_input = np.interp(x_new, x_old, raw_input).astype(np.float32)

        denoised_float = raw_input

        _nr = self.nr_mode
        if _nr == 2 and self.dfn_engine:
            try:
                denoised_float = self.dfn_engine.process(denoised_float)
            except Exception:
                pass
        elif _nr == 1 and self.denoiser:
            try:
                np.multiply(denoised_float, 32767.0,
                            out=self._pcm_int16_buf, casting='unsafe')
                processed = [f.flatten() for p, f in
                             self.denoiser.denoise_chunk(self._pcm_int16_buf)]
                if processed:
                    combined = np.concatenate(processed) if len(processed) > 1 else processed[0]
                    denoised_float = combined.astype(np.float32) / 32767.0
            except Exception:
                pass

        _in_peak = np.max(np.abs(denoised_float))
        if _in_peak > 0.98:
            denoised_float = denoised_float * (0.98 / _in_peak)

        rms = np.sqrt(np.mean(denoised_float ** 2))
        self._vol_emit_counter += 1
        if self._vol_emit_counter >= 5:
            self._vol_emit_counter = 0
            self.volume_level_signal.emit(int(min(rms * 1000, 100)))

        is_talking = rms > self.vad_threshold or (curr_time - self.last_voice_time < self.vad_hangover)
        if rms > self.vad_threshold:
            self.last_voice_time = curr_time

        if self.my_uid != 0:
            mute_flag = 1 if self._is_muted.is_set() else 0
            deaf_flag = 2 if self._is_deafened.is_set() else 0
            flags = mute_flag | deaf_flag
            whisper_uid = self.whisper_target_uid

            try:
                if is_talking and whisper_uid != 0:
                    np.multiply(denoised_float, 32767, out=self._pcm_int16_buf,
                                casting='unsafe')
                    encoded = self.encoder.encode(self._pcm_int16_buf.tobytes(), CHUNK_SIZE)
                    self._whisper_sequence += 1
                    w_flags = FLAG_WHISPER
                    if self._whisper_anonymous:
                        w_flags |= FLAG_ANONYMOUS
                    w_header = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time,
                                                      self._whisper_sequence, w_flags)
                    w_payload = struct.pack('!I', whisper_uid) + encoded
                    try:
                        self.send_queue.put_nowait(w_header + w_payload)
                    except Exception:
                        pass

                elif is_talking and not self._is_muted.is_set():
                    np.multiply(denoised_float, 32767, out=self._pcm_int16_buf,
                                casting='unsafe')
                    encoded = self.encoder.encode(self._pcm_int16_buf.tobytes(), CHUNK_SIZE)
                    self.my_sequence += 1
                    packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time,
                                                    self.my_sequence, flags) + encoded
                    if not self.was_talking:
                        while self.vad_pre_buffer:
                            try:
                                self.send_queue.put_nowait(self.vad_pre_buffer.popleft())
                            except Exception:
                                pass
                        self.was_talking = True
                    self.send_queue.put_nowait(packet)

                else:
                    self.was_talking = False
                    if not is_talking and whisper_uid == 0:
                        empty_packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time, 0, flags)
                        self.vad_pre_buffer.append(empty_packet)
            except Exception:
                pass

    def _output_callback(self, outdata, frames, time_info, status):
        if not self._cb_out_first_logged:
            self._cb_out_first_logged = True
            print("[DEBUG] _output_callback: ПЕРВЫЙ ВЫЗОВ — OutputStream (WASAPI) работает", flush=True)
        if status:
            print(f"[DEBUG] _output_callback: status={status}", flush=True)

        if not self._is_running.is_set():
            outdata.fill(0)
            return

        curr_time = time.perf_counter()

        _diag_voice_frame:  float = 0.0
        _diag_stream_frame: float = 0.0

        self.mix_buffer.fill(0)
        _n_active = 0
        if not self._is_deafened.is_set():
            _n_active = sum(
                1 for u in self._audio_users_snapshot.values()
                if (curr_time - u.last_packet_time < 0.4
                    and not u.is_locally_muted
                    and not u.volume_zero)
            )

            if _n_active <= 2:
                _speaker_gain = 1.0
            else:
                _speaker_gain = max(0.75, 1.0 - 0.1 * (_n_active - 2))

            for uid, user in self._audio_users_snapshot.items():
                if curr_time - user.last_packet_time < 1.5:
                    data = user.jitter_buffer.get()
                    if not user.is_locally_muted and not user.volume_zero:
                        try:
                            if data:
                                decoded = user.decoder.decode(data, CHUNK_SIZE)
                                s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0
                                _w_ts = self._active_whispers.get(uid, 0.0)
                                if _w_ts and (curr_time - _w_ts) < 2.0:
                                    if uid not in self._whisper_states:
                                        _warm_history = np.resize(
                                            s.astype(np.float32), 2048).copy()
                                        _N = CHUNK_SIZE
                                        self._whisper_states[uid] = {
                                            'history': _warm_history,
                                            'phase':   0.0,
                                            'lp_zi':   np.zeros(
                                                (self._anon_lp_sos.shape[0], 2),
                                                dtype=np.float64),
                                            'buf':     np.zeros(
                                                2048 + _N, dtype=np.float32),
                                            '_arange': np.arange(_N, dtype=np.float64),
                                            '_base':   np.arange(2048, 2048 + _N, dtype=np.float64),
                                        }
                                    s = self._apply_anonymous_voice_effect(
                                        s, self._whisper_states[uid])
                                else:
                                    self._active_whispers.pop(uid, None)
                                    self._whisper_states.pop(uid, None)

                                self.mix_buffer += s * (user.volume * _speaker_gain)
                                if AUDIO_DIAG_ENABLED:
                                    _diag_voice_frame += float(np.dot(s, s)) / CHUNK_SIZE
                            else:
                                try:
                                    user.decoder.decode(None, CHUNK_SIZE)
                                except Exception:
                                    pass
                        except Exception:
                            pass

        with self._local_sounds_lock:
            active = []
            for snd in self._local_sounds:
                pos  = snd['pos']
                rem  = len(snd['data']) - pos
                take = min(CHUNK_SIZE, rem)
                if take > 0:
                    self.mix_buffer[:take] += snd['data'][pos:pos + take] * snd['vol']
                    snd['pos'] += take
                    if snd['pos'] < len(snd['data']):
                        active.append(snd)
            self._local_sounds = active

        if self._stream_active:
            with self._stream_lock:
                avail = self._stream_fill
                if avail >= CHUNK_SIZE:
                    _current_vol = self._stream_vol   # FIX: убрано * 0.4
                    self.mix_buffer += self._stream_buf[:CHUNK_SIZE] * _current_vol
                    if AUDIO_DIAG_ENABLED:
                        _sb = self._stream_buf[:CHUNK_SIZE]
                        _diag_stream_frame = float(np.dot(_sb, _sb)) / CHUNK_SIZE
                    remaining = avail - CHUNK_SIZE
                    if remaining > 0:
                        self._stream_buf[:remaining] = self._stream_buf[CHUNK_SIZE:avail]
                    self._stream_fill = remaining
                elif avail > 0:
                    _current_vol = self._stream_vol   # FIX: убрано * 0.4
                    _fade = np.linspace(1.0, 0.0, avail, dtype=np.float32)
                    self.mix_buffer[:avail] += (
                        self._stream_buf[:avail] * _fade * _current_vol
                    )
                    self._stream_fill = 0

        _limit = 0.95
        _over  = np.abs(self.mix_buffer) > _limit
        if np.any(_over):
            _excess = np.abs(self.mix_buffer[_over]) - _limit
            self.mix_buffer[_over] = (
                np.sign(self.mix_buffer[_over])
                * (_limit + (1.0 - _limit) * np.tanh(_excess / (1.0 - _limit)))
            )
        np.clip(self.mix_buffer, -1.0, 1.0, out=self.mix_buffer)

        if AUDIO_DIAG_ENABLED:
            _mix_rms_sq = float(np.dot(self.mix_buffer, self.mix_buffer)) / CHUNK_SIZE
            self._diag_voice_sum  += _diag_voice_frame
            self._diag_stream_sum += _diag_stream_frame
            self._diag_mix_sum    += _mix_rms_sq
            self._diag_cnt        += 1

            if curr_time >= self._diag_next_ts and self._diag_cnt > 0:
                self._diag_voice_sum  = 0.0
                self._diag_stream_sum = 0.0
                self._diag_mix_sum    = 0.0
                self._diag_cnt        = 0
                self._diag_next_ts    = curr_time + 1.0

        if self._out_sr != SAMPLE_RATE:
            resampled = None
            try:
                from scipy.signal import resample_poly as _rp
                g = math.gcd(int(self._out_sr), int(SAMPLE_RATE))
                up   = int(self._out_sr) // g
                down = int(SAMPLE_RATE)  // g
                resampled = _rp(self.mix_buffer, up, down).astype(np.float32)
            except Exception:
                x_old = np.linspace(0.0, 1.0, CHUNK_SIZE,         dtype=np.float64)
                x_new = np.linspace(0.0, 1.0, self._out_blocksize, dtype=np.float64)
                resampled = np.interp(x_new, x_old, self.mix_buffer).astype(np.float32)
            outdata[:] = resampled[:frames].reshape(-1, 1)
        else:
            outdata[:] = self.mix_buffer.reshape(-1, 1)

    def register_ip_mapping(self, uid, ip_addr):
        if not ip_addr: return
        with self.users_lock:
            self.uid_to_ip[uid] = ip_addr
            saved_vol = self.settings.value(f"vol_ip_{ip_addr}", None)
            if saved_vol is not None:
                saved_vol = float(saved_vol)
                if uid in self.remote_users:
                    self.remote_users[uid].volume = saved_vol
                    self.remote_users[uid].volume_zero = (saved_vol == 0.0)
                else:
                    self.pending_volumes[uid] = saved_vol

    def set_user_volume(self, uid, vol):

        vol = max(0.0, min(20.0, float(vol)))
        emit_zero_state = None

        with self.users_lock:
            if uid in self.remote_users:
                user = self.remote_users[uid]
                prev_zero = user.volume_zero
                user.volume = vol
                user.volume_zero = (vol == 0.0)
                if user.volume_zero != prev_zero:
                    emit_zero_state = user.volume_zero
                ip = self.uid_to_ip.get(uid)
                if ip:
                    self.settings.setValue(f"vol_ip_{ip}", vol)
                else:
                    self.settings.setValue(f"volume_{uid}", vol)

        if emit_zero_state is not None:
            self.user_volume_zero.emit(uid, emit_zero_state)

    def toggle_user_mute(self, uid):
        with self.users_lock:
            if uid in self.remote_users:
                self.remote_users[uid].is_locally_muted = not self.remote_users[uid].is_locally_muted
                return self.remote_users[uid].is_locally_muted
        return False

    def start_whisper(self, target_uid: int, anonymous: bool = False):
        self.whisper_target_uid = target_uid
        self._whisper_anonymous = bool(anonymous)
        self._whisper_sequence = self.my_sequence
        self._wlp_zi = sosfilt_zi(self._wlp_sos).astype(np.float64)
        self._whisper_effect_reset = True
        print(f"[Audio] Whisper START → uid={target_uid}, anon={self._whisper_anonymous}, "
              f"seq_from={self._whisper_sequence}")

    def stop_whisper(self):
        if self._whisper_sequence > self.my_sequence:
            self.my_sequence = self._whisper_sequence
        print(f"[Audio] Whisper STOP  (was → uid={self.whisper_target_uid}, "
              f"anon={self._whisper_anonymous}), seq_sync={self.my_sequence}")
        self.whisper_target_uid = 0
        self._whisper_anonymous = False

    def add_incoming_packet(self, uid, seq, data, flags=0):
        try:
            self.incoming_packets.put_nowait((uid, seq, data, flags))
        except:
            pass

    def add_incoming_whisper_packet(self, uid, seq, data):
        now = time.perf_counter()

        self._active_whispers[uid] = now

        self._whisper_in_uid = uid
        self._whisper_in_ts  = now

        self.whisper_received.emit(uid)

        self.add_incoming_packet(uid, seq, data, 0)

    def _remap_to_wasapi(self, mme_idx: int | None) -> int | None:
        try:
            apis = sd.query_hostapis()
            devices = sd.query_devices()

            wasapi_host_idx = next(
                (i for i, a in enumerate(apis) if 'WASAPI' in a['name']), None
            )

            if mme_idx is None:
                if wasapi_host_idx is not None:
                    default_wasapi_out = apis[wasapi_host_idx]['default_output_device']
                    print(f"[Audio] Дефолтное устройство → WASAPI (idx {default_wasapi_out})")
                    return default_wasapi_out
                return None

            dev = devices[mme_idx]
            host_api = apis[dev['hostapi']]

            if 'MME' not in host_api['name']:
                return mme_idx  # уже не MME

            if wasapi_host_idx is None:
                print("[Audio] WASAPI недоступен, остаёмся на MME (возможно эхо)")
                return mme_idx

            default_wasapi = apis[wasapi_host_idx]['default_output_device']
            phys_name = dev['name']
            best_idx = None
            best_score = -1

            for i, d in enumerate(devices):
                if d['hostapi'] != wasapi_host_idx or d['max_output_channels'] <= 0:
                    continue
                wname = d['name']
                if wname == phys_name:
                    best_idx = i
                    break
                match_len = min(len(phys_name), len(wname), 10)
                if match_len >= 8 and (
                        wname.startswith(phys_name[:match_len]) or
                        phys_name.startswith(wname[:match_len])
                ):
                    score = match_len
                    if score > best_score:
                        best_score = score
                        best_idx = i

            if best_idx is not None:
                print(
                    f"[Audio] Output MME→WASAPI: "
                    f"idx {mme_idx}→{best_idx} "
                    f"'{phys_name}' → '{devices[best_idx]['name']}'"
                )
                return best_idx

            print(
                f"[Audio] WASAPI-аналог для '{phys_name}' не найден. "
                f"Форсируем дефолтный WASAPI (idx {default_wasapi})"
            )
            return default_wasapi  # ← единственная важная строка

        except Exception as e:
            print(f"[Audio] _remap_to_wasapi error: {e}")
            return mme_idx

    @property
    def is_muted(self):
        return self._is_muted.is_set()

    @is_muted.setter
    def is_muted(self, value):
        if value:
            self._is_muted.set()
        else:
            self._is_muted.clear()
        self.status_changed.emit(self.is_muted, self.is_deafened)

    @property
    def is_deafened(self):
        return self._is_deafened.is_set()

    @is_deafened.setter
    def is_deafened(self, value):
        if value:
            self._is_deafened.set()
        else:
            self._is_deafened.clear()
        self.status_changed.emit(self.is_muted, self.is_deafened)