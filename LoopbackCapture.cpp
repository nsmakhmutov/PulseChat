// ============================================================================
//  LoopbackCapture.cpp  —  InPulse Audio Exclusion DLL
//  Сборка: Visual Studio, Release, x64
//  Линковать: Mmdevapi.lib  Audioclient.lib  Ole32.lib
//
//  API:
//    StartCapture(DWORD exclude_pid, AudioCallback callback) -> bool
//    StopCapture()
//
//  Захватывает системный звук с дефолтного устройства вывода (WASAPI Loopback),
//  полностью исключая дерево процессов с PID = exclude_pid (т.е. сам InPulse).
//  Зрители трансляции не услышат голосовой чат / системные звуки самого клиента.
//
//  Требует Windows 10 Build 20348+ / Windows 11.
//  AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK — нативный API без виртуальных
//  кабелей. Discord и OBS используют ровно тот же механизм.
// ============================================================================

// Минимальная версия Windows для AUDIOCLIENT_ACTIVATION_PARAMS
#ifndef WINVER
#   define WINVER 0x0A00
#endif
#ifndef _WIN32_WINNT
#   define _WIN32_WINNT 0x0A00
#endif

// AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS доступен только начиная с SDK 10.0.20348
// Если сборка упадёт здесь — обновите Windows SDK в Visual Studio Installer.
#define NTDDI_VERSION 0x0A00A000   // NTDDI_WIN10_FE  (SDK 20348)

#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <audioclientactivationparams.h>
#include <functiondiscoverykeys_devpkey.h>

#include <thread>
#include <atomic>
#include <cstring>
#include <cstdio>

// ----------------------------------------------------------------------------
//  Тип колбэка, который вызывается из C++ → Python ctypes
//  float* pcm_data   — интерливед float32 (L,R,L,R... или моно)
//  int    num_frames — количество аудиофреймов в буфере
//  int    channels   — 1 или 2 (DLL не конвертирует, Python сам смешает)
//  int    sample_rate — нативная частота устройства (обычно 48000)
// ----------------------------------------------------------------------------
typedef void (*AudioCallback)(float* pcm_data, int num_frames,
    int channels, int sample_rate);

// ----------------------------------------------------------------------------
//  Глобальное состояние (единственный экземпляр захвата)
// ----------------------------------------------------------------------------
namespace {
    std::atomic<bool>  g_running{ false };
    std::thread        g_thread;
    AudioCallback      g_callback = nullptr;
    DWORD              g_exclude_pid = 0;

    // Вспомогательный макрос — печатает ошибку и возвращает false/void
#define CHECK_HR(hr, msg)  \
        if (FAILED(hr)) {      \
            printf("[DLL] ERROR %s  HRESULT=0x%08X\n", (msg), (unsigned)(hr)); \
            return false;      \
        }
}

// ----------------------------------------------------------------------------
//  Рабочий поток захвата
// ----------------------------------------------------------------------------
static void CaptureThread()
{
    // ── 1. Инициализация COM для этого потока ────────────────────────────────
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        printf("[DLL] CoInitializeEx failed: 0x%08X\n", (unsigned)hr);
        return;
    }

    IMMDeviceEnumerator* pEnumerator = nullptr;
    IMMDevice* pDevice = nullptr;
    IAudioClient* pAudioClient = nullptr;
    IAudioCaptureClient* pCaptureClient = nullptr;

    auto Cleanup = [&]() {
        if (pCaptureClient) { pCaptureClient->Release(); pCaptureClient = nullptr; }
        if (pAudioClient) {
            pAudioClient->Stop();
            pAudioClient->Release();   pAudioClient = nullptr;
        }
        if (pDevice) { pDevice->Release();         pDevice = nullptr; }
        if (pEnumerator) { pEnumerator->Release();     pEnumerator = nullptr; }
        CoUninitialize();
        };

    // ── 2. Получить дефолтное устройство вывода (Render / динамики) ──────────
    hr = CoCreateInstance(
        __uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
        __uuidof(IMMDeviceEnumerator), (void**)&pEnumerator);
    if (FAILED(hr)) {
        printf("[DLL] CoCreateInstance(MMDeviceEnumerator) failed: 0x%08X\n", (unsigned)hr);
        CoUninitialize();
        return;
    }

    hr = pEnumerator->GetDefaultAudioEndpoint(eRender, eConsole, &pDevice);
    if (FAILED(hr)) {
        printf("[DLL] GetDefaultAudioEndpoint failed: 0x%08X\n", (unsigned)hr);
        Cleanup();
        return;
    }

    // ── 3. Создать IAudioClient через ActivateAudioInterfaceAsync-подобный путь
    //       с параметрами Process Loopback (исключение процесса InPulse) ───────
    //
    //  AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK — единственный способ
    //  получить loopback с исключением конкретного PID без виртуальных кабелей.
    //  Метод IMMDevice::Activate с этим параметром поддерживается с SDK 20348.
    //
    AUDIOCLIENT_ACTIVATION_PARAMS activationParams = {};
    activationParams.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    activationParams.ProcessLoopbackParams.TargetProcessId = g_exclude_pid;
    activationParams.ProcessLoopbackParams.ProcessLoopbackMode =
        PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE;
    //  EXCLUDE_TARGET_PROCESS_TREE — исключает сам процесс + все его дочерние.
    //  Это важно: PySide6 / Python subprocess тоже будут исключены автоматически.

    PROPVARIANT activatePropVar = {};
    activatePropVar.vt = VT_BLOB;
    activatePropVar.blob.cbSize = sizeof(activationParams);
    activatePropVar.blob.pBlobData = (BYTE*)&activationParams;

    hr = pDevice->Activate(
        __uuidof(IAudioClient),
        CLSCTX_ALL,
        &activatePropVar,
        (void**)&pAudioClient);
    if (FAILED(hr)) {
        // 0x88890008 = AUDCLNT_E_DEVICE_INVALIDATED  (устройство недоступно)
        // 0x80070057 = E_INVALIDARG (старый SDK — нет поддержки этого API)
        printf("[DLL] IMMDevice::Activate (ProcessLoopback) failed: 0x%08X\n"
            "      Требуется Windows 10 build 20348+ и SDK >= 10.0.20348\n",
            (unsigned)hr);
        Cleanup();
        return;
    }
    printf("[DLL] IAudioClient активирован с Process Loopback (exclude PID=%lu)\n",
        (unsigned long)g_exclude_pid);

    // ── 4. Получить смешанный формат устройства ───────────────────────────────
    WAVEFORMATEX* pMixFormat = nullptr;
    hr = pAudioClient->GetMixFormat(&pMixFormat);
    if (FAILED(hr)) {
        printf("[DLL] GetMixFormat failed: 0x%08X\n", (unsigned)hr);
        Cleanup();
        return;
    }

    // Запрашиваем float32; если устройство отдаёт другой формат — принуждаем.
    // WASAPI Shared всегда поддерживает IEEE_FLOAT на современных Windows.
    WAVEFORMATEXTENSIBLE wfex = {};
    if (pMixFormat->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        wfex = *(WAVEFORMATEXTENSIBLE*)pMixFormat;
    }
    else {
        wfex.Format = *pMixFormat;
        wfex.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
        wfex.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
        wfex.dwChannelMask = (pMixFormat->nChannels == 2)
            ? SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT
            : SPEAKER_FRONT_CENTER;
        wfex.Samples.wValidBitsPerSample = 32;
        wfex.SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
    }
    // Принудительно float32
    wfex.Format.wBitsPerSample = 32;
    wfex.Format.nBlockAlign = wfex.Format.nChannels * 4;
    wfex.Format.nAvgBytesPerSec = wfex.Format.nSamplesPerSec * wfex.Format.nBlockAlign;
    wfex.Samples.wValidBitsPerSample = 32;
    wfex.SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;

    const int nChannels = wfex.Format.nChannels;
    const int nSampleRate = (int)wfex.Format.nSamplesPerSec;

    CoTaskMemFree(pMixFormat);

    // ── 5. Инициализация IAudioClient (Shared, Event-driven) ─────────────────
    //  AUDCLNT_STREAMFLAGS_LOOPBACK  — разрешает loopback-захват с Render endpoint.
    //  hnsBufferDuration = 200 ms — запас для случаев высокой нагрузки.
    const REFERENCE_TIME hnsBuffer = 2000000; // 200 мс (единицы 100нс)

    hr = pAudioClient->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK,
        hnsBuffer,
        0,
        (WAVEFORMATEX*)&wfex,
        nullptr);
    if (FAILED(hr)) {
        printf("[DLL] IAudioClient::Initialize failed: 0x%08X\n", (unsigned)hr);
        Cleanup();
        return;
    }

    // ── 6. Получить IAudioCaptureClient ──────────────────────────────────────
    hr = pAudioClient->GetService(__uuidof(IAudioCaptureClient),
        (void**)&pCaptureClient);
    if (FAILED(hr)) {
        printf("[DLL] GetService(IAudioCaptureClient) failed: 0x%08X\n", (unsigned)hr);
        Cleanup();
        return;
    }

    hr = pAudioClient->Start();
    if (FAILED(hr)) {
        printf("[DLL] IAudioClient::Start failed: 0x%08X\n", (unsigned)hr);
        Cleanup();
        return;
    }

    printf("[DLL] ✔ Захват запущен: %d Гц, %d кан., float32\n", nSampleRate, nChannels);

    // ── 7. Петля захвата ──────────────────────────────────────────────────────
    //  Опрос каждые 10 мс — баланс между задержкой и нагрузкой на CPU.
    //  WASAPI Loopback не поддерживает event-driven режим в SHARED mode,
    //  поэтому используем polling (это нормальная практика для OBS/Discord).
    while (g_running.load(std::memory_order_relaxed))
    {
        UINT32 packetLength = 0;
        hr = pCaptureClient->GetNextPacketSize(&packetLength);
        if (FAILED(hr)) {
            printf("[DLL] GetNextPacketSize failed: 0x%08X — прерывание\n", (unsigned)hr);
            break;
        }

        while (packetLength > 0)
        {
            BYTE* pData = nullptr;
            UINT32 numFrames = 0;
            DWORD  dwFlags = 0;

            hr = pCaptureClient->GetBuffer(&pData, &numFrames, &dwFlags, nullptr, nullptr);
            if (FAILED(hr)) {
                printf("[DLL] GetBuffer failed: 0x%08X\n", (unsigned)hr);
                break;
            }

            // AUDCLNT_BUFFERFLAGS_SILENT — устройство молчит (нет звука).
            // Передаём нулевой буфер в Python, чтобы не сбивать синхронизацию.
            if (dwFlags & AUDCLNT_BUFFERFLAGS_SILENT) {
                // Заполняем нулями, чтобы Python получил тишину, а не мусор
                memset(pData, 0, numFrames * nChannels * sizeof(float));
            }

            // Вызываем Python-колбэк напрямую из этого потока.
            // ctypes CFUNCTYPE (GIL освобождается автоматически для таких вызовов).
            if (g_callback && numFrames > 0) {
                g_callback(
                    reinterpret_cast<float*>(pData),
                    (int)numFrames,
                    nChannels,
                    nSampleRate
                );
            }

            hr = pCaptureClient->ReleaseBuffer(numFrames);
            if (FAILED(hr)) {
                printf("[DLL] ReleaseBuffer failed: 0x%08X\n", (unsigned)hr);
                break;
            }

            hr = pCaptureClient->GetNextPacketSize(&packetLength);
            if (FAILED(hr)) {
                packetLength = 0;
                break;
            }
        }

        // Короткий сон между опросами буфера
        Sleep(10);
    }

    printf("[DLL] Петля захвата завершена, освобождаю ресурсы...\n");
    Cleanup();
}

// ============================================================================
//  Экспортируемый C-интерфейс
// ============================================================================

extern "C" {

    // ----------------------------------------------------------------------------
    //  StartCapture
    //  exclude_pid — PID процесса, чей звук нужно ИСКЛЮЧИТЬ (os.getpid() из Python)
    //  callback    — Python ctypes CFUNCTYPE, вызывается на каждый буфер WASAPI
    //  Возвращает true если поток захвата успешно запущен.
    // ----------------------------------------------------------------------------
    __declspec(dllexport)
        bool StartCapture(DWORD exclude_pid, AudioCallback callback)
    {
        if (g_running.load()) {
            printf("[DLL] StartCapture: захват уже запущен, сначала вызови StopCapture()\n");
            return false;
        }
        if (!callback) {
            printf("[DLL] StartCapture: callback не может быть nullptr\n");
            return false;
        }

        g_exclude_pid = exclude_pid;
        g_callback = callback;
        g_running.store(true, std::memory_order_release);

        // Запускаем рабочий поток
        g_thread = std::thread(CaptureThread);

        printf("[DLL] StartCapture: поток запущен (exclude PID=%lu)\n",
            (unsigned long)exclude_pid);
        return true;
    }

    // ----------------------------------------------------------------------------
    //  StopCapture
    //  Корректно сигнализирует потоку завершиться и ждёт его (join).
    //  COM-ресурсы и IAudioClient освобождаются внутри потока.
    // ----------------------------------------------------------------------------
    __declspec(dllexport)
        void StopCapture()
    {
        if (!g_running.load()) {
            return; // уже остановлен
        }
        printf("[DLL] StopCapture: сигнал остановки...\n");
        g_running.store(false, std::memory_order_release);

        if (g_thread.joinable()) {
            g_thread.join();
        }

        g_callback = nullptr;
        g_exclude_pid = 0;
        printf("[DLL] StopCapture: поток завершён\n");
    }

} // extern "C"