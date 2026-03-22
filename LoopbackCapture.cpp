// ============================================================================
//  LoopbackCapture.cpp  —  InPulse Audio Exclusion DLL  v4 (Full)
//  Сборка: Visual Studio, Release, x64
//  Линковать: Mmdevapi.lib  Audioclient.lib  Ole32.lib
//  Имя выходного файла: InPulseAudioExclusion.dll
//    (Project → Properties → Linker → General → Output File)
//
//  ── CAPTURE API ──────────────────────────────────────────────────────────
//    StartCapture(DWORD exclude_pid, AudioCallback callback) -> bool
//    StopCapture()
//
//  ── RENDER API ───────────────────────────────────────────────────────────
//    StartRender()                                           -> bool
//    StopRender()
//    PlayAudioChunk(float* pcm, int num_frames,
//                  int src_channels, int src_sr)            -> bool
//    IsRawModeActive()                                       -> bool
//    IsWasapiReady()                                         -> bool
//    IsRenderPollingMode()                                   -> bool
//    ReadRenderedAudio(float* dst, int max_frames,
//                      int dst_channels)                    -> int
//    GetRenderedAudioAvailable()                             -> int
//
//  ── ЧТО ИСПРАВЛЕНО В v4 (CAPTURE) ───────────────────────────────────────
//
//  ПРОБЛЕМА v3: IMMDevice::Activate() с AUDIOCLIENT_ACTIVATION_PARAMS и типом
//  AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK НЕ ПОДДЕРЖИВАЕТСЯ.
//
//  Документация Microsoft (audioclientactivationparams.h):
//    "AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK — This activation type is
//     ONLY supported when using ActivateAudioInterfaceAsync to activate an
//     IAudioClient."
//
//  Старый код вызывал pDevice->Activate() с параметрами Process Loopback.
//  Windows принимал вызов (S_OK), но МОЛЧА ИГНОРИРОВАЛ параметры exclusion —
//  создавался обычный loopback БЕЗ исключения PID.
//  Результат: зрители слышали свои голоса через стрим (эхо).
//
//  ИСПРАВЛЕНИЕ v4:
//    1. Добавлен класс AudioActivationHandler — реализует
//       IActivateAudioInterfaceCompletionHandler (без WRL/wil).
//    2. CaptureThread теперь вызывает ActivateAudioInterfaceAsync()
//       с VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK в качестве device path.
//    3. Синхронное ожидание завершения через WaitForSingleObject(event, 5000).
//
//  ── ЧТО ИСПРАВЛЕНО В v3 (RENDER) ────────────────────────────────────────
//
//  v2 вызывал CoInitializeEx/CoCreateInstance прямо в StartRender() —
//  из Python ctypes потока (aiortc async event loop). Этот поток НЕ является
//  COM-потоком, что приводило к:
//    "OSError: exception: access violation reading 0x0000000000000000"
//
//  ИСПРАВЛЕНИЕ: весь COM/WASAPI перенесён в RenderThread().
//  StartRender() теперь ТОЛЬКО:
//    1. Сбрасывает кольцевой буфер
//    2. Создаёт HANDLE event (для синхронизации со StopRender)
//    3. Устанавливает g_running = true
//    4. Запускает std::thread(RenderThread)
//    5. Возвращает true
//
//  ПОРЯДОК вызовов (критичен для PID атрибуции Windows Audio Graph):
//    StartRender() → StartCapture()   [render-сессия ПЕРЕД loopback захватом]
//    StopCapture() → StopRender()
//
//  Требует Windows 10 Build 20348+ / Windows 11 (SDK >= 10.0.20348).
// ============================================================================

#ifndef WINVER
#   define WINVER 0x0A00
#endif
#ifndef _WIN32_WINNT
#   define _WIN32_WINNT 0x0A00
#endif
#define NTDDI_VERSION 0x0A00A000   // NTDDI_WIN10_FE (SDK 20348)

#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <audioclientactivationparams.h>
#include <functiondiscoverykeys_devpkey.h>
#include <audiosessiontypes.h>

#include <thread>
#include <atomic>
#include <mutex>
#include <algorithm>
#include <cstring>
#include <cstdio>

#ifndef AUDCLNT_STREAMFLAGS_RAW
#define AUDCLNT_STREAMFLAGS_RAW 0x08000000
#endif

#ifndef AUDCLNT_STREAMOPTIONS_RAW
#define AUDCLNT_STREAMOPTIONS_RAW 0x0002
#endif

// Путь виртуального устройства для Process Loopback.
// Это специальный GUID — не реальное аудиоустройство.
// ActivateAudioInterfaceAsync принимает его как deviceInterfacePath.
#ifndef VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
#define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK \
    L"{2eef81be-33fa-4800-9670-1cd474972c3f}"
#endif

// ============================================================================
//  AudioActivationHandler
//
//  Реализует IActivateAudioInterfaceCompletionHandler без WRL/wil.
//  ActivateAudioInterfaceAsync вызывает ActivateCompleted() в threadpool
//  потоке после завершения инициализации IAudioClient.
//
//  Синхронизация: SetEvent(m_hEvent) в callback → CaptureThread ждёт через
//  WaitForSingleObject(m_hEvent, timeout).
//
//  Время жизни: создаётся на куче (new), управляется AddRef/Release.
//  После Release() объект самоуничтожается.
// ============================================================================
class AudioActivationHandler final :
    public IActivateAudioInterfaceCompletionHandler,
    public IAgileObject   // REQUIRED: without this WinRT returns E_ILLEGAL_METHOD_CALL
{
public:
    HANDLE        m_hEvent = nullptr;
    IAudioClient* m_pClient = nullptr;
    HRESULT       m_hrResult = E_PENDING;

    AudioActivationHandler()
        : m_cRef(1)
        , m_hEvent(CreateEvent(nullptr, FALSE, FALSE, nullptr))
    {
    }

    ~AudioActivationHandler()
    {
        if (m_pClient) { m_pClient->Release(); m_pClient = nullptr; }
        if (m_hEvent) { CloseHandle(m_hEvent); m_hEvent = nullptr; }
    }

    // ── IUnknown ─────────────────────────────────────────────────────────────
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown ||
            riid == __uuidof(IActivateAudioInterfaceCompletionHandler) ||
            riid == __uuidof(IAgileObject))
        {
            *ppv = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }

    STDMETHODIMP_(ULONG) AddRef() override
    {
        return InterlockedIncrement(&m_cRef);
    }

    STDMETHODIMP_(ULONG) Release() override
    {
        ULONG r = InterlockedDecrement(&m_cRef);
        if (r == 0) delete this;
        return r;
    }

    // ── IActivateAudioInterfaceCompletionHandler ──────────────────────────────
    // Вызывается Windows threadpool'ом после завершения активации.
    STDMETHODIMP ActivateCompleted(
        IActivateAudioInterfaceAsyncOperation* pOperation) override
    {
        HRESULT hrActivate = E_FAIL;
        IUnknown* pUnknown = nullptr;

        HRESULT hr = pOperation->GetActivateResult(&hrActivate, &pUnknown);
        if (SUCCEEDED(hr) && SUCCEEDED(hrActivate) && pUnknown)
        {
            hr = pUnknown->QueryInterface(__uuidof(IAudioClient),
                reinterpret_cast<void**>(&m_pClient));
            if (FAILED(hr))
            {
                printf("[DLL] ActivateCompleted: QI(IAudioClient) failed: 0x%08X\n",
                    (unsigned)hr);
                hrActivate = hr;
            }
            pUnknown->Release();
        }
        else if (SUCCEEDED(hr) && FAILED(hrActivate))
        {
            printf("[DLL] ActivateCompleted: activation result failed: 0x%08X\n",
                (unsigned)hrActivate);
        }

        m_hrResult = hrActivate;
        if (m_hEvent) SetEvent(m_hEvent);
        return S_OK;
    }

private:
    volatile LONG m_cRef;
};


// ============================================================================
//  CAPTURE  —  исправленный CaptureThread (v4)
// ============================================================================

typedef void (*AudioCallback)(float* pcm_data, int num_frames,
    int channels, int sample_rate);

namespace {
    std::atomic<bool>  g_cap_running{ false };
    std::thread        g_cap_thread;
    AudioCallback      g_callback = nullptr;
    DWORD              g_exclude_pid = 0;
}

static void CaptureThread()
{
    // ── COM инициализация ─────────────────────────────────────────────────────
    // COINIT_MULTITHREADED (MTA) совместим с ActivateAudioInterfaceAsync:
    // callback вызывается в threadpool потоке Windows — SetEvent() thread-safe.
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        printf("[DLL] CaptureThread: CoInitializeEx failed: 0x%08X\n", (unsigned)hr);
        return;
    }

    IAudioClient* pAudioClient = nullptr;
    IAudioCaptureClient* pCaptureClient = nullptr;

    // IMMDevice и IMMDeviceEnumerator не нужны —
    // ActivateAudioInterfaceAsync принимает device path напрямую.
    auto Cleanup = [&]() {
        if (pCaptureClient) { pCaptureClient->Release(); pCaptureClient = nullptr; }
        if (pAudioClient) { pAudioClient->Stop(); pAudioClient->Release(); pAudioClient = nullptr; }
        CoUninitialize();
        };

    // ── Process Loopback Capture через ActivateAudioInterfaceAsync ─────────────
    //
    //  КАК ЭТО РАБОТАЕТ:
    //  1. Устанавливаем ProcessLoopbackParams: исключить дерево процессов python.exe.
    //  2. ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ...)
    //     — Windows создаёт IAudioClient для виртуального loopback устройства
    //     с заданными параметрами exclusion.
    //  3. Виртуальный GUID — не реальный endpoint. Windows маршрутизирует его
    //     к дефолтному render endpoint автоматически.
    //  4. Ждём callback через AudioActivationHandler (WaitForSingleObject).
    //  5. После получения IAudioClient — стандартная WASAPI loopback инициализация.

    AUDIOCLIENT_ACTIVATION_PARAMS ap = {};
    ap.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    ap.ProcessLoopbackParams.TargetProcessId = g_exclude_pid;
    ap.ProcessLoopbackParams.ProcessLoopbackMode =
        PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE;

    PROPVARIANT pv = {};
    pv.vt = VT_BLOB;
    pv.blob.cbSize = sizeof(ap);
    pv.blob.pBlobData = reinterpret_cast<BYTE*>(&ap);

    AudioActivationHandler* pHandler = new AudioActivationHandler();
    if (!pHandler || !pHandler->m_hEvent)
    {
        printf("[DLL] CaptureThread: не удалось создать AudioActivationHandler\n");
        if (pHandler) pHandler->Release();
        CoUninitialize();
        return;
    }

    // pAsyncOp держим живым до callback — ранний Release() может закрыть
    // контекст операции до доставки ActivateCompleted() на некоторых билдах.
    IActivateAudioInterfaceAsyncOperation* pAsyncOp = nullptr;

    hr = ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        __uuidof(IAudioClient),
        &pv,
        pHandler,
        &pAsyncOp
    );

    if (FAILED(hr))
    {
        printf("[DLL] CaptureThread: ActivateAudioInterfaceAsync failed: 0x%08X\n"
            "      Требуется Windows 10 Build 20348+ / Windows 11, SDK >= 10.0.20348\n",
            (unsigned)hr);
        if (pAsyncOp) { pAsyncOp->Release(); pAsyncOp = nullptr; }
        pHandler->Release();
        CoUninitialize();
        return;
    }

    // Ждём callback. MsgWaitForMultipleObjects качает очередь сообщений —
    // нужно для STA, безвредно для MTA.
    {
        DWORD deadline  = GetTickCount() + 5000;
        DWORD dwWait    = WAIT_TIMEOUT;
        while (true) {
            DWORD now  = GetTickCount();
            DWORD left = (now < deadline) ? (deadline - now) : 0;
            dwWait = MsgWaitForMultipleObjects(1, &pHandler->m_hEvent, FALSE, left, QS_ALLEVENTS);
            if (dwWait == WAIT_OBJECT_0) break;
            if (dwWait == WAIT_OBJECT_0 + 1) {
                MSG msg;
                while (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) {
                    TranslateMessage(&msg); DispatchMessage(&msg);
                }
                if (left == 0) { dwWait = WAIT_TIMEOUT; break; }
                continue;
            }
            break;
        }
        if (dwWait != WAIT_OBJECT_0) {
            printf("[DLL] CaptureThread: timeout waiting for IAudioClient (dwWait=0x%lX)\n",
                (unsigned long)dwWait);
            if (pAsyncOp) { pAsyncOp->Release(); pAsyncOp = nullptr; }
            pHandler->Release();
            CoUninitialize();
            return;
        }
    }

    // Callback получен — теперь можно освободить pAsyncOp
    if (pAsyncOp) { pAsyncOp->Release(); pAsyncOp = nullptr; }

    if (FAILED(pHandler->m_hrResult) || !pHandler->m_pClient)
    {
        printf("[DLL] CaptureThread: Process Loopback activation failed: 0x%08X\n"
            "      1. Windows < Build 20348 (requires Win10 20H2+ or Win11)\n"
            "      2. SDK < 10.0.20348 at compile time\n"
            "      3. Try running as Administrator\n",
            (unsigned)pHandler->m_hrResult);
        pHandler->Release();
        CoUninitialize();
        return;
    }

    // Забираем IAudioClient из handler (transfer ownership)
    pAudioClient = pHandler->m_pClient;
    pHandler->m_pClient = nullptr;
    pHandler->Release();
    pHandler = nullptr;

    printf("[DLL] IAudioClient активирован: Process Loopback Exclude PID=%lu\n"
        "      Голоса InPulse будут исключены из захвата.\n",
        (unsigned long)g_exclude_pid);

    // ── Стандартная WASAPI loopback инициализация ──────────────────────────────

    // GetMixFormat на виртуальном Process Loopback GUID возвращает E_NOTIMPL.
    // Берём формат у реального дефолтного Render эндпоинта — аудиодвижок
    // всё равно ресемплирует все потоки к этому формату.
    WAVEFORMATEX* pMixFormat = nullptr;
    {
        IMMDeviceEnumerator* pEnum = nullptr;
        IMMDevice*           pDev  = nullptr;
        IAudioClient*        pTmp  = nullptr;
        HRESULT hrFmt = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr,
                            CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&pEnum);
        if (SUCCEEDED(hrFmt)) hrFmt = pEnum->GetDefaultAudioEndpoint(eRender, eConsole, &pDev);
        if (SUCCEEDED(hrFmt)) hrFmt = pDev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&pTmp);
        if (SUCCEEDED(hrFmt)) hrFmt = pTmp->GetMixFormat(&pMixFormat);
        if (pTmp)  pTmp->Release();
        if (pDev)  pDev->Release();
        if (pEnum) pEnum->Release();
        if (FAILED(hrFmt) || !pMixFormat)  // fallback: try virtual client directly
            hrFmt = pAudioClient->GetMixFormat(&pMixFormat);
        if (FAILED(hrFmt) || !pMixFormat) {
            printf("[DLL] GetMixFormat failed: 0x%08X\n", (unsigned)hrFmt);
            Cleanup(); return;
        }
    }

    WAVEFORMATEXTENSIBLE wfex = {};
    if (pMixFormat->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        wfex = *reinterpret_cast<WAVEFORMATEXTENSIBLE*>(pMixFormat);
    }
    else {
        wfex.Format = *pMixFormat;
        wfex.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
        wfex.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
        wfex.dwChannelMask = (pMixFormat->nChannels == 2)
            ? SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT : SPEAKER_FRONT_CENTER;
        wfex.Samples.wValidBitsPerSample = 32;
        wfex.SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
    }
    wfex.Format.wBitsPerSample = 32;
    wfex.Format.nBlockAlign = wfex.Format.nChannels * 4;
    wfex.Format.nAvgBytesPerSec = wfex.Format.nSamplesPerSec * wfex.Format.nBlockAlign;
    wfex.Samples.wValidBitsPerSample = 32;
    wfex.SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;

    const int nChannels = wfex.Format.nChannels;
    const int nSampleRate = static_cast<int>(wfex.Format.nSamplesPerSec);
    CoTaskMemFree(pMixFormat);

    // AUDCLNT_STREAMFLAGS_LOOPBACK обязателен для IAudioCaptureClient на render endpoint.
    hr = pAudioClient->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK,
        2000000, 0,
        reinterpret_cast<WAVEFORMATEX*>(&wfex),
        nullptr
    );
    if (FAILED(hr)) {
        printf("[DLL] Capture Initialize failed: 0x%08X\n", (unsigned)hr);
        Cleanup(); return;
    }

    hr = pAudioClient->GetService(__uuidof(IAudioCaptureClient),
        reinterpret_cast<void**>(&pCaptureClient));
    if (FAILED(hr)) {
        printf("[DLL] GetService(IAudioCaptureClient) failed: 0x%08X\n", (unsigned)hr);
        Cleanup(); return;
    }

    hr = pAudioClient->Start();
    if (FAILED(hr)) {
        printf("[DLL] Capture Start failed: 0x%08X\n", (unsigned)hr);
        Cleanup(); return;
    }

    printf("[DLL] Захват запущен: %d Гц, %d кан., float32\n"
        "[DLL] PID %lu исключён из loopback (голоса InPulse НЕ попадут в стрим)\n",
        nSampleRate, nChannels, (unsigned long)g_exclude_pid);

    // ── Основной цикл захвата ─────────────────────────────────────────────────
    while (g_cap_running.load(std::memory_order_relaxed))
    {
        UINT32 packetLength = 0;
        hr = pCaptureClient->GetNextPacketSize(&packetLength);
        if (FAILED(hr)) {
            printf("[DLL] GetNextPacketSize failed: 0x%08X\n", (unsigned)hr);
            break;
        }

        while (packetLength > 0)
        {
            BYTE* pData = nullptr;
            UINT32 numFrames = 0;
            DWORD  dwFlags = 0;
            hr = pCaptureClient->GetBuffer(&pData, &numFrames, &dwFlags,
                nullptr, nullptr);
            if (FAILED(hr)) {
                printf("[DLL] GetBuffer failed: 0x%08X\n", (unsigned)hr);
                goto capture_loop_exit;
            }

            if (dwFlags & AUDCLNT_BUFFERFLAGS_SILENT)
                memset(pData, 0, numFrames * nChannels * sizeof(float));

            if (g_callback && numFrames > 0)
                g_callback(reinterpret_cast<float*>(pData),
                    static_cast<int>(numFrames), nChannels, nSampleRate);

            hr = pCaptureClient->ReleaseBuffer(numFrames);
            if (FAILED(hr)) {
                printf("[DLL] ReleaseBuffer failed: 0x%08X\n", (unsigned)hr);
                goto capture_loop_exit;
            }

            hr = pCaptureClient->GetNextPacketSize(&packetLength);
            if (FAILED(hr)) { packetLength = 0; break; }
        }
        Sleep(10);
    }

capture_loop_exit:
    printf("[DLL] CaptureThread завершён\n");
    Cleanup();
}


// ============================================================================
//  RENDER ENGINE  —  RAW WASAPI (обход APO/audiodg.exe)
//
//  ВСЕ COM/WASAPI вызовы выполняются внутри RenderThread().
//  StartRender() — ТОЛЬКО: reset кольца + CreateEvent + g_running=true + std::thread.
//  Никаких COM на вызывающем потоке Python.
// ============================================================================

namespace RenderEngine {

    // Кольцевой буфер (ring)
    static const int RING_FRAMES = 65536;   // ~1.36 с при 48 кГц
    static const int MAX_CH = 8;

    float      g_ring[RING_FRAMES * MAX_CH];
    int        g_ring_write = 0;
    int        g_ring_read = 0;
    int        g_ring_avail = 0;
    std::mutex g_ring_mtx;

    // WASAPI объекты — создаются и уничтожаются ВНУТРИ RenderThread
    IAudioClient* g_client = nullptr;
    IAudioRenderClient* g_render = nullptr;
    UINT32              g_buf_sz = 0;

    // g_event: создаётся в StartRender(), уничтожается в StopRender() после join
    HANDLE g_event = nullptr;

    // Параметры открытого устройства (заполняется RenderThread)
    int g_dev_channels = 2;
    int g_dev_sr = 48000;

    // Управление потоком
    std::atomic<bool> g_running{ false };
    std::thread       g_thread;

    // Результат RAW init
    std::atomic<bool> g_raw_active{ false };

    // Флаг успешной инициализации WASAPI
    std::atomic<bool> g_wasapi_ready{ false };

    // Polling mode: true когда RAW получен без AUDCLNT_STREAMFLAGS_EVENTCALLBACK
    std::atomic<bool> g_render_polling{ false };

    // ── Render tap (software EC reference для Python) ─────────────────────────
    // Размер: 9600 фреймов = 200 мс @ 48 кГц
    static const int TAP_FRAMES = 9600;
    float      g_tap[TAP_FRAMES * MAX_CH] = {};
    int        g_tap_w = 0;
    int        g_tap_avail = 0;
    std::mutex g_tap_mtx;

} // namespace RenderEngine


// ---------------------------------------------------------------------------
//  Вспомогательные функции — вызываются ТОЛЬКО из RenderThread
// ---------------------------------------------------------------------------

static WAVEFORMATEXTENSIBLE BuildFloat32Format(const WAVEFORMATEX* pMix)
{
    WAVEFORMATEXTENSIBLE wfex = {};
    if (pMix->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        wfex = *(const WAVEFORMATEXTENSIBLE*)pMix;
    }
    else {
        wfex.Format = *pMix;
        wfex.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
        wfex.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
        wfex.dwChannelMask = (pMix->nChannels == 2)
            ? SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT : SPEAKER_FRONT_CENTER;
    }
    wfex.Format.wBitsPerSample = 32;
    wfex.Format.nBlockAlign = wfex.Format.nChannels * 4;
    wfex.Format.nAvgBytesPerSec = wfex.Format.nSamplesPerSec * wfex.Format.nBlockAlign;
    wfex.Samples.wValidBitsPerSample = 32;
    wfex.SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
    return wfex;
}

static IMMDevice* GetDefaultRenderDevice()
{
    IMMDeviceEnumerator* pEnum = nullptr;
    IMMDevice* pDev = nullptr;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
        __uuidof(IMMDeviceEnumerator), (void**)&pEnum);
    if (FAILED(hr)) {
        printf("[DLL-Render] GetDefaultRenderDevice: CoCreateInstance failed: 0x%08X\n", (unsigned)hr);
        return nullptr;
    }
    hr = pEnum->GetDefaultAudioEndpoint(eRender, eConsole, &pDev);
    pEnum->Release();
    if (FAILED(hr)) {
        printf("[DLL-Render] GetDefaultRenderDevice: GetDefaultAudioEndpoint failed: 0x%08X\n", (unsigned)hr);
        return nullptr;
    }
    return pDev;
}

static IAudioClient* AcquireNewClient()
{
    IMMDevice* pDev = GetDefaultRenderDevice();
    if (!pDev) return nullptr;
    IAudioClient* pC = nullptr;
    HRESULT hr = pDev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&pC);
    pDev->Release();
    return SUCCEEDED(hr) ? pC : nullptr;
}

// ============================================================================
//  RenderThread  —  ВСЕ COM и WASAPI вызовы происходят здесь
// ============================================================================

static void RenderThread()
{
    using namespace RenderEngine;

    // ── 1. COM инициализация ────────────────────────────────────────────────
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        printf("[DLL-Render] RenderThread: CoInitializeEx failed: 0x%08X\n", (unsigned)hr);
        g_running.store(false, std::memory_order_release);
        return;
    }

    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);

    // ── 2. Получить устройство ──────────────────────────────────────────────
    IMMDevice* pDevice = GetDefaultRenderDevice();
    if (!pDevice) {
        printf("[DLL-Render] RenderThread: нет дефолтного Render устройства\n");
        CoUninitialize();
        g_running.store(false, std::memory_order_release);
        return;
    }

    // ── 3. IAudioClient ─────────────────────────────────────────────────────
    IAudioClient* pClient = nullptr;
    hr = pDevice->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&pClient);
    pDevice->Release(); pDevice = nullptr;
    if (FAILED(hr)) {
        printf("[DLL-Render] RenderThread: Activate(IAudioClient) failed: 0x%08X\n", (unsigned)hr);
        CoUninitialize();
        g_running.store(false, std::memory_order_release);
        return;
    }

    // ── 4. GetMixFormat ─────────────────────────────────────────────────────
    WAVEFORMATEX* pMix = nullptr;
    hr = pClient->GetMixFormat(&pMix);
    if (FAILED(hr)) {
        printf("[DLL-Render] RenderThread: GetMixFormat failed: 0x%08X\n", (unsigned)hr);
        pClient->Release();
        CoUninitialize();
        g_running.store(false, std::memory_order_release);
        return;
    }
    g_dev_channels = pMix->nChannels;
    if (g_dev_channels > MAX_CH) g_dev_channels = MAX_CH;
    g_dev_sr = (int)pMix->nSamplesPerSec;
    WAVEFORMATEXTENSIBLE wfex = BuildFloat32Format(pMix);
    CoTaskMemFree(pMix);

    // ── 5. Initialize: ищем RAW mode тремя способами ────────────────────────
    //
    //  ПОЧЕМУ RAW НЕОБХОДИМ:
    //  В Shared mode аудио идёт через audiodg.exe + APO.
    //  После APO-обработки PID атрибуция сессии теряется — WASAPI присваивает
    //  аудио audiodg.exe, а не нашему процессу.
    //  PROCESS_LOOPBACK_EXCLUDE(pid) ищет наш PID → не находит → НЕ исключает
    //  → loopback захватывает голоса → эхо.
    //
    //  В RAW mode: аудио идёт напрямую в hardware, минуя audiodg.exe.
    //  PID сохраняется → PROCESS_LOOPBACK_EXCLUDE работает корректно.
    //
    //  Порядок попыток:
    //    A) IAudioClient2::SetClientProperties(RAW) + Initialize(EVENTCALLBACK)
    //    B) Initialize(EVENTCALLBACK | AUDCLNT_STREAMFLAGS_RAW)
    //    C) Initialize(AUDCLNT_STREAMFLAGS_RAW) без EVENTCALLBACK [polling]
    //    D) Initialize(EVENTCALLBACK) — Shared без RAW (last resort)

    bool raw_obtained = false;
    bool polling_mode = false;

    // ── Попытка A: IAudioClient2::SetClientProperties(AUDCLNT_STREAMOPTIONS_RAW)
    {
        IAudioClient2* pC2 = nullptr;
        if (SUCCEEDED(pClient->QueryInterface(__uuidof(IAudioClient2), (void**)&pC2))) {
            AudioClientProperties props = {};
            props.cbSize = sizeof(AudioClientProperties);
            props.bIsOffload = FALSE;
            props.eCategory = AudioCategory_Media;
            props.Options = static_cast<AUDCLNT_STREAMOPTIONS>(AUDCLNT_STREAMOPTIONS_RAW);
            HRESULT hr2 = pC2->SetClientProperties(&props);
            pC2->Release();

            if (SUCCEEDED(hr2)) {
                hr = pClient->Initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                    2000000, 0, (WAVEFORMATEX*)&wfex, nullptr);

                if (SUCCEEDED(hr)) {
                    raw_obtained = true;
                    printf("[DLL-Render] ✔ RAW mode via IAudioClient2::SetClientProperties"
                        " — APO bypass, эхо устранено\n");
                }
                else {
                    printf("[DLL-Render] Initialize после SetClientProperties(RAW) failed:"
                        " 0x%08X — пересоздаём IAudioClient\n", (unsigned)hr);
                    pClient->Release(); pClient = nullptr;
                    pClient = AcquireNewClient();
                }
            }
            else {
                printf("[DLL-Render] SetClientProperties(AUDCLNT_STREAMOPTIONS_RAW) failed:"
                    " 0x%08X\n", (unsigned)hr2);
            }
        }
    }

    // ── Попытка B: AUDCLNT_STREAMFLAGS_RAW | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
    if (!raw_obtained && pClient) {
        hr = pClient->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_RAW,
            2000000, 0, (WAVEFORMATEX*)&wfex, nullptr);

        if (SUCCEEDED(hr)) {
            raw_obtained = true;
            printf("[DLL-Render] ✔ RAW mode via Initialize flags (legacy event-driven)\n");
        }
        else {
            printf("[DLL-Render] Initialize(RAW|EVENTCALLBACK) failed: 0x%08X"
                " — пробуем polling\n", (unsigned)hr);
            pClient->Release(); pClient = nullptr;
            pClient = AcquireNewClient();
        }
    }

    // ── Попытка C: AUDCLNT_STREAMFLAGS_RAW без EVENTCALLBACK (polling mode)
    if (!raw_obtained && pClient) {
        hr = pClient->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_RAW,
            500000, 0, (WAVEFORMATEX*)&wfex, nullptr);   // 50 мс буфер

        if (SUCCEEDED(hr)) {
            raw_obtained = true;
            polling_mode = true;
            printf("[DLL-Render] ✔ RAW mode (polling, без event callback)"
                " — APO bypass активен\n");
        }
        else {
            printf("[DLL-Render] Initialize(RAW polling) failed: 0x%08X"
                " — fallback Shared без RAW\n", (unsigned)hr);
            pClient->Release(); pClient = nullptr;
            pClient = AcquireNewClient();
        }
    }

    // ── Попытка D: Shared без RAW (last resort)
    if (!raw_obtained && pClient) {
        hr = pClient->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            2000000, 0, (WAVEFORMATEX*)&wfex, nullptr);

        if (SUCCEEDED(hr)) {
            printf("[DLL-Render] ⚠ Shared без RAW (все RAW-попытки провалились)\n"
                "             Эхо возможно. Отключите 'Улучшения звука' в\n"
                "             Параметры → Звук → [устройство] → Доп.параметры → Улучшения\n");
        }
        else {
            printf("[DLL-Render] Initialize Shared fallback failed: 0x%08X\n", (unsigned)hr);
            if (pClient) { pClient->Release(); pClient = nullptr; }
            CoUninitialize();
            g_running.store(false, std::memory_order_release);
            return;
        }
    }

    if (!pClient) {
        printf("[DLL-Render] Ни одна стратегия Initialize не сработала — выход\n");
        CoUninitialize();
        g_running.store(false, std::memory_order_release);
        return;
    }

    g_raw_active.store(raw_obtained, std::memory_order_release);
    g_render_polling.store(polling_mode, std::memory_order_release);

    // ── 6. SetEventHandle (только для event-driven режима) ──────────────────
    if (!polling_mode) {
        hr = pClient->SetEventHandle(g_event);
        if (FAILED(hr)) {
            printf("[DLL-Render] RenderThread: SetEventHandle failed: 0x%08X\n", (unsigned)hr);
            pClient->Release();
            CoUninitialize();
            g_running.store(false, std::memory_order_release);
            return;
        }
    }

    // ── 7. IAudioRenderClient ───────────────────────────────────────────────
    IAudioRenderClient* pRender = nullptr;
    hr = pClient->GetService(__uuidof(IAudioRenderClient), (void**)&pRender);
    if (FAILED(hr)) {
        printf("[DLL-Render] RenderThread: GetService(IAudioRenderClient) failed: 0x%08X\n", (unsigned)hr);
        pClient->Release();
        CoUninitialize();
        g_running.store(false, std::memory_order_release);
        return;
    }

    UINT32 bufSz = 0;
    pClient->GetBufferSize(&bufSz);
    g_buf_sz = bufSz;
    g_client = pClient;
    g_render = pRender;

    // ── 8. Start ────────────────────────────────────────────────────────────
    hr = pClient->Start();
    if (FAILED(hr)) {
        printf("[DLL-Render] RenderThread: IAudioClient::Start failed: 0x%08X\n", (unsigned)hr);
        pRender->Release(); g_render = nullptr;
        pClient->Release(); g_client = nullptr;
        CoUninitialize();
        g_running.store(false, std::memory_order_release);
        return;
    }

    g_wasapi_ready.store(true, std::memory_order_release);
    printf("[DLL-Render] ✔ Render запущен: %d Гц, %d кан., float32, RAW=%s, mode=%s\n",
        g_dev_sr, g_dev_channels,
        g_raw_active.load() ? "YES" : "NO",
        polling_mode ? "polling" : "event-driven");

    // ── 9. Основной цикл воспроизведения ────────────────────────────────────
    while (g_running.load(std::memory_order_relaxed))
    {
        if (!polling_mode) {
            DWORD res = WaitForSingleObject(g_event, 200);
            if (!g_running.load()) break;
            if (res == WAIT_TIMEOUT) continue;
        }
        else {
            Sleep(5);
            if (!g_running.load()) break;
        }

        UINT32 padding = 0;
        hr = g_client->GetCurrentPadding(&padding);
        if (FAILED(hr)) {
            printf("[DLL-Render] GetCurrentPadding failed: 0x%08X\n", (unsigned)hr);
            break;
        }

        UINT32 available = g_buf_sz - padding;
        if (available == 0) continue;

        BYTE* pData = nullptr;
        hr = g_render->GetBuffer(available, &pData);
        if (hr == AUDCLNT_E_BUFFER_TOO_LARGE) {
            available = g_buf_sz / 2;
            hr = g_render->GetBuffer(available, &pData);
        }
        if (FAILED(hr)) {
            printf("[DLL-Render] GetBuffer failed: 0x%08X\n", (unsigned)hr);
            break;
        }

        float* fData = reinterpret_cast<float*>(pData);
        int    filled = 0;
        {
            std::lock_guard<std::mutex> lk(g_ring_mtx);
            int n = (std::min)((int)available, g_ring_avail);
            for (int f = 0; f < n; ++f) {
                int base = g_ring_read * g_dev_channels;
                for (int c = 0; c < g_dev_channels; ++c)
                    fData[f * g_dev_channels + c] = g_ring[base + c];
                g_ring_read = (g_ring_read + 1) % RING_FRAMES;
            }
            g_ring_avail -= n;
            filled = n;
        }
        // Underrun: тишина вместо мусора
        for (UINT32 f = (UINT32)filled; f < available; ++f)
            for (int c = 0; c < g_dev_channels; ++c)
                fData[f * g_dev_channels + c] = 0.0f;

        // ── Render tap ───────────────────────────────────────────────────────
        if (filled > 0) {
            std::lock_guard<std::mutex> lk_tap(g_tap_mtx);
            for (int f = 0; f < filled; ++f) {
                int tbase = g_tap_w * g_dev_channels;
                int sbase = f * g_dev_channels;
                for (int c = 0; c < g_dev_channels; ++c)
                    g_tap[tbase + c] = fData[sbase + c];
                g_tap_w = (g_tap_w + 1) % TAP_FRAMES;
            }
            g_tap_avail = (std::min)(g_tap_avail + filled, TAP_FRAMES);
        }

        hr = g_render->ReleaseBuffer(available, 0);
        if (FAILED(hr)) {
            printf("[DLL-Render] ReleaseBuffer failed: 0x%08X\n", (unsigned)hr);
            break;
        }
    }

    // ── 10. Очистка WASAPI (внутри потока) ──────────────────────────────────
    if (g_client) { g_client->Stop(); g_client->Release(); g_client = nullptr; }
    if (g_render) { g_render->Release(); g_render = nullptr; }
    // g_event НЕ закрываем здесь — его закрывает StopRender() после join()

    g_wasapi_ready.store(false, std::memory_order_release);
    g_raw_active.store(false, std::memory_order_release);

    printf("[DLL-Render] RenderThread завершён\n");
    CoUninitialize();
}


// ============================================================================
//  Экспортируемый C-интерфейс
// ============================================================================
extern "C" {

    // ─── CAPTURE ────────────────────────────────────────────────────────────────

    __declspec(dllexport)
        bool StartCapture(DWORD exclude_pid, AudioCallback callback)
    {
        if (g_cap_running.load()) {
            printf("[DLL] StartCapture: уже запущен\n");
            return false;
        }
        if (!callback) {
            printf("[DLL] StartCapture: callback == nullptr\n");
            return false;
        }
        g_exclude_pid = exclude_pid;
        g_callback = callback;
        g_cap_running.store(true, std::memory_order_release);
        g_cap_thread = std::thread(CaptureThread);
        printf("[DLL] StartCapture: поток запущен (exclude PID=%lu)\n",
            (unsigned long)exclude_pid);
        return true;
    }

    __declspec(dllexport)
        void StopCapture()
    {
        if (!g_cap_running.load()) return;
        printf("[DLL] StopCapture: остановка...\n");
        g_cap_running.store(false, std::memory_order_release);
        if (g_cap_thread.joinable()) g_cap_thread.join();
        g_callback = nullptr;
        g_exclude_pid = 0;
        printf("[DLL] StopCapture: завершено\n");
    }

    // ─── RENDER ENGINE ──────────────────────────────────────────────────────────

    // ---------------------------------------------------------------------------
    //  StartRender
    //
    //  ТОЛЬКО: reset ring + CreateEvent + g_running=true + запуск RenderThread.
    //  НЕТ COM вызовов на вызывающем потоке Python.
    //  RenderThread сам инициализирует COM и WASAPI в своём потоке.
    //
    //  ВЫЗЫВАТЬ ДО StartCapture().
    // ---------------------------------------------------------------------------
    __declspec(dllexport)
        bool StartRender()
    {
        using namespace RenderEngine;

        if (g_running.load()) {
            printf("[DLL-Render] StartRender: уже запущен\n");
            return true;
        }

        // Сброс кольцевого буфера
        {
            std::lock_guard<std::mutex> lk(g_ring_mtx);
            g_ring_write = 0; g_ring_read = 0; g_ring_avail = 0;
            memset(g_ring, 0, sizeof(g_ring));
        }

        g_raw_active.store(false, std::memory_order_release);
        g_wasapi_ready.store(false, std::memory_order_release);
        g_render_polling.store(false, std::memory_order_release);
        g_buf_sz = 0;
        g_client = nullptr;
        g_render = nullptr;

        // Сброс tap буфера
        {
            std::lock_guard<std::mutex> lk_tap(g_tap_mtx);
            g_tap_w = 0; g_tap_avail = 0;
            memset(g_tap, 0, sizeof(g_tap));
        }

        // Создаём event ДО запуска потока
        g_event = CreateEvent(nullptr, FALSE, FALSE, nullptr);
        if (!g_event) {
            printf("[DLL-Render] StartRender: CreateEvent failed (GLE=%lu)\n",
                (unsigned long)GetLastError());
            return false;
        }

        g_running.store(true, std::memory_order_release);
        g_thread = std::thread(RenderThread);

        printf("[DLL-Render] StartRender: поток запущен (WASAPI init в фоне)\n");
        return true;
    }

    // ---------------------------------------------------------------------------
    //  StopRender
    // ---------------------------------------------------------------------------
    __declspec(dllexport)
        void StopRender()
    {
        using namespace RenderEngine;

        if (!g_running.load()) return;

        printf("[DLL-Render] StopRender: остановка...\n");
        g_running.store(false, std::memory_order_release);

        if (g_event) SetEvent(g_event);
        if (g_thread.joinable()) g_thread.join();

        // Закрываем event ПОСЛЕ join
        if (g_event) { CloseHandle(g_event); g_event = nullptr; }

        printf("[DLL-Render] StopRender: завершено\n");
    }

    // ---------------------------------------------------------------------------
    //  PlayAudioChunk
    //
    //  Пишет float32 PCM в кольцевой буфер.
    //  Вызывается из Python _output_callback каждые 20 мс.
    // ---------------------------------------------------------------------------
    __declspec(dllexport)
        bool PlayAudioChunk(float* pcm, int num_frames, int src_channels, int src_sr)
    {
        using namespace RenderEngine;

        if (!g_running.load() || !pcm || num_frames <= 0) return false;
        if (src_channels < 1) src_channels = 1;
        if (src_channels > MAX_CH) src_channels = MAX_CH;

        std::lock_guard<std::mutex> lk(g_ring_mtx);

        // Освобождаем место при переполнении (предпочитаем свежие данные)
        int space = RING_FRAMES - g_ring_avail;
        if (space < num_frames) {
            int drop = num_frames - space;
            g_ring_read = (g_ring_read + drop) % RING_FRAMES;
            g_ring_avail = (std::max)(0, g_ring_avail - drop);
        }

        // Запись в кольцо с конвертацией каналов моно→N
        for (int f = 0; f < num_frames; ++f) {
            int ring_base = g_ring_write * g_dev_channels;
            for (int c = 0; c < g_dev_channels; ++c) {
                int sc = (c < src_channels) ? c : (src_channels - 1);
                g_ring[ring_base + c] = pcm[f * src_channels + sc];
            }
            g_ring_write = (g_ring_write + 1) % RING_FRAMES;
        }
        g_ring_avail += num_frames;
        return true;
    }

    // ---------------------------------------------------------------------------
    //  IsRawModeActive
    // ---------------------------------------------------------------------------
    __declspec(dllexport)
        bool IsRawModeActive()
    {
        return RenderEngine::g_raw_active.load(std::memory_order_acquire);
    }

    // ---------------------------------------------------------------------------
    //  IsWasapiReady
    // ---------------------------------------------------------------------------
    __declspec(dllexport)
        bool IsWasapiReady()
    {
        return RenderEngine::g_wasapi_ready.load(std::memory_order_acquire);
    }

    // ---------------------------------------------------------------------------
    //  IsRenderPollingMode
    // ---------------------------------------------------------------------------
    __declspec(dllexport)
        bool IsRenderPollingMode()
    {
        return RenderEngine::g_render_polling.load(std::memory_order_acquire);
    }

    // ---------------------------------------------------------------------------
    //  ReadRenderedAudio
    //
    //  Читает из render tap ring buffer для software echo cancellation.
    //  dst_channels == 1 → микс каналов в моно.
    // ---------------------------------------------------------------------------
    __declspec(dllexport)
        int ReadRenderedAudio(float* dst, int max_frames, int dst_channels)
    {
        using namespace RenderEngine;
        if (!dst || max_frames <= 0 || dst_channels < 1) return 0;
        std::lock_guard<std::mutex> lk(g_tap_mtx);
        int n = (std::min)(max_frames, g_tap_avail);
        if (n <= 0) return 0;
        int rpos = (g_tap_w - g_tap_avail + TAP_FRAMES) % TAP_FRAMES;
        for (int f = 0; f < n; ++f) {
            int sbase = rpos * g_dev_channels;
            if (dst_channels == 1) {
                float s = 0.0f;
                for (int c = 0; c < g_dev_channels; ++c)
                    s += g_tap[sbase + c];
                dst[f] = s / (float)g_dev_channels;
            }
            else {
                int ch = (std::min)(dst_channels, g_dev_channels);
                for (int c = 0; c < ch; ++c)
                    dst[f * dst_channels + c] = g_tap[sbase + c];
                for (int c = g_dev_channels; c < dst_channels; ++c)
                    dst[f * dst_channels + c] = 0.0f;
            }
            rpos = (rpos + 1) % TAP_FRAMES;
        }
        g_tap_avail -= n;
        return n;
    }

    // ---------------------------------------------------------------------------
    //  GetRenderedAudioAvailable
    // ---------------------------------------------------------------------------
    __declspec(dllexport)
        int GetRenderedAudioAvailable()
    {
        using namespace RenderEngine;
        std::lock_guard<std::mutex> lk(g_tap_mtx);
        return g_tap_avail;
    }

} // extern "C"