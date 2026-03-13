/**
 * loopback_capture.cpp — FULL & COMPILES
 * WASAPI Application Loopback API. Исключает звук нашего процесса.
 */

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <roapi.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <wrl.h>
#include <wrl/client.h>
#include <wrl/implements.h>
#include <vector>
#include <thread>
#include <atomic>
#include <mutex>
#include <cstdio>

using namespace Microsoft::WRL;

// --- Жёстко задаём структуры Win11, чтобы избежать ошибок компилятора ---
#ifndef VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
#define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"
#endif

#ifndef AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
typedef enum AUDIOCLIENT_ACTIVATION_TYPE {
    AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT = 0,
    AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
} AUDIOCLIENT_ACTIVATION_TYPE;

typedef enum PROCESS_LOOPBACK_MODE {
    PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0,
    PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
} PROCESS_LOOPBACK_MODE;

typedef struct AUDIOCLIENT_ACTIVATION_PARAMS {
    AUDIOCLIENT_ACTIVATION_TYPE ActivationType;
    union {
        struct {
            DWORD TargetProcessId;
            PROCESS_LOOPBACK_MODE ProcessLoopbackMode;
        } ProcessLoopbackParams;
    };
} AUDIOCLIENT_ACTIVATION_PARAMS;
#endif

// --- Глобальные переменные ---
ComPtr<IAudioClient> g_audio_client;
ComPtr<IAudioCaptureClient> g_capture_client;
std::atomic<bool> g_running{ false };
std::thread g_thread;
std::vector<float> g_ring;
std::mutex g_ring_mtx;
HANDLE g_init_done = nullptr;
std::atomic<HRESULT> g_init_hr{ E_FAIL };

char g_log_buf[2048] = { 0 };
size_t g_log_pos = 0;

void dll_log(const char* fmt, ...) {
    char tmp[512];
    va_list args;
    va_start(args, fmt);
    vsnprintf(tmp, sizeof(tmp), fmt, args);
    va_end(args);

    std::lock_guard<std::mutex> lk(g_ring_mtx);
    size_t len = strlen(tmp);
    if (g_log_pos + len + 1 < sizeof(g_log_buf)) {
        memcpy(g_log_buf + g_log_pos, tmp, len);
        g_log_pos += len;
        g_log_buf[g_log_pos] = '\0';
    }
}

// --- Обработчик активации интерфейса (FtmBase делает его IAgileObject) ---
class ActivationHandler : public RuntimeClass<RuntimeClassFlags<ClassicCom>, IActivateAudioInterfaceCompletionHandler, FtmBase> {
public:
    HANDLE m_hEvent;
    HRESULT m_hrActivateResult;

    ActivationHandler() : m_hrActivateResult(E_FAIL) {
        m_hEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    }
    ~ActivationHandler() {
        if (m_hEvent) CloseHandle(m_hEvent);
    }

    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* op) override {
        HRESULT hrActivate = S_OK;
        ComPtr<IUnknown> unknown;

        HRESULT hr = op->GetActivateResult(&hrActivate, &unknown);
        if (SUCCEEDED(hr) && SUCCEEDED(hrActivate)) {
            hr = unknown.As(&g_audio_client);
            if (SUCCEEDED(hr)) {
                m_hrActivateResult = S_OK;
            } else {
                m_hrActivateResult = hr;
            }
        } else {
            m_hrActivateResult = FAILED(hr) ? hr : hrActivate;
        }
        SetEvent(m_hEvent);
        return S_OK;
    }
};

void CaptureThread(DWORD exclude_pid) {
    HRESULT hr = RoInitialize(RO_INIT_MULTITHREADED);
    bool ro_init = (hr == S_OK || hr == S_FALSE);

    AUDIOCLIENT_ACTIVATION_PARAMS params = { AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT };
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = exclude_pid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE;

    PROPVARIANT activateParams = { 0 };
    activateParams.vt = VT_BLOB;
    activateParams.blob.cbSize = sizeof(params);
    activateParams.blob.pBlobData = reinterpret_cast<BYTE*>(&params);

    auto handler = Make<ActivationHandler>();
    ComPtr<IActivateAudioInterfaceAsyncOperation> asyncOp;

    hr = ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        __uuidof(IAudioClient),
        &activateParams,
        handler.Get(),
        &asyncOp
    );

    if (FAILED(hr)) {
        dll_log("ActivateAudioInterfaceAsync failed: 0x%08X\n", hr);
        g_init_hr.store(hr);
        if (ro_init) RoUninitialize();
        SetEvent(g_init_done);
        return;
    }

    // Ждём завершения коллбэка
    WaitForSingleObject(handler->m_hEvent, 5000);
    if (FAILED(handler->m_hrActivateResult)) {
        dll_log("Activation callback failed: 0x%08X\n", handler->m_hrActivateResult);
        g_init_hr.store(handler->m_hrActivateResult);
        if (ro_init) RoUninitialize();
        SetEvent(g_init_done);
        return;
    }

    WAVEFORMATEX* pwfx = nullptr;
    hr = g_audio_client->GetMixFormat(&pwfx);
    if (FAILED(hr)) {
        g_init_hr.store(hr);
        SetEvent(g_init_done);
        return;
    }

    hr = g_audio_client->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
        0, 0, pwfx, nullptr
    );

    if (FAILED(hr)) {
        dll_log("Initialize failed: 0x%08X\n", hr);
        g_init_hr.store(hr);
        CoTaskMemFree(pwfx);
        if (ro_init) RoUninitialize();
        SetEvent(g_init_done);
        return;
    }

    HANDLE hEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    g_audio_client->SetEventHandle(hEvent);
    g_audio_client->GetService(IID_PPV_ARGS(&g_capture_client));
    g_audio_client->Start();

    g_init_hr.store(S_OK);
    SetEvent(g_init_done);
    dll_log("Capture running. Excluded PID: %u\n", exclude_pid);

    UINT32 channels = pwfx->nChannels;

    // Основной цикл захвата
    while (g_running.load()) {
        DWORD waitResult = WaitForSingleObject(hEvent, 100);
        if (waitResult != WAIT_OBJECT_0) continue;

        UINT32 framesAvailable = 0;
        hr = g_capture_client->GetNextPacketSize(&framesAvailable);
        if (FAILED(hr)) break;

        while (framesAvailable > 0) {
            BYTE* pData;
            DWORD flags;
            hr = g_capture_client->GetBuffer(&pData, &framesAvailable, &flags, nullptr, nullptr);
            if (SUCCEEDED(hr)) {
                if (!(flags & AUDCLNT_BUFFERFLAGS_SILENT)) {
                    std::lock_guard<std::mutex> lk(g_ring_mtx);
                    float* fData = reinterpret_cast<float*>(pData);

                    for (UINT32 i = 0; i < framesAvailable; i++) {
                        float sum = 0.0f;
                        for (UINT32 c = 0; c < channels; c++) {
                            sum += fData[i * channels + c];
                        }
                        g_ring.push_back(sum / static_cast<float>(channels));
                    }
                }
                g_capture_client->ReleaseBuffer(framesAvailable);
            }
            g_capture_client->GetNextPacketSize(&framesAvailable);
        }
    }

    g_audio_client->Stop();
    CoTaskMemFree(pwfx);
    CloseHandle(hEvent);
    if (ro_init) RoUninitialize();
}

extern "C" {
    __declspec(dllexport) bool is_supported() {
        return true;
    }

    __declspec(dllexport) const char* get_last_log() {
        return g_log_buf;
    }

    __declspec(dllexport) int init_capture(DWORD exclude_pid) {
        if (g_running.load()) return 0;

        g_log_pos = 0;
        memset(g_log_buf, 0, sizeof(g_log_buf));
        g_ring.clear();

        g_init_done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        g_init_hr.store(E_FAIL);

        g_running.store(true);
        g_thread = std::thread(CaptureThread, exclude_pid);

        WaitForSingleObject(g_init_done, 5000);
        CloseHandle(g_init_done);

        int result = static_cast<int>(g_init_hr.load());
        if (result != 0) {
            g_running.store(false);
            if (g_thread.joinable()) g_thread.join();
        }
        return result;
    }

    __declspec(dllexport) int read_frames(float* out, int n) {
        std::lock_guard<std::mutex> lk(g_ring_mtx);
        if (g_ring.size() < static_cast<size_t>(n)) return 0;

        memcpy(out, g_ring.data(), n * sizeof(float));
        g_ring.erase(g_ring.begin(), g_ring.begin() + n);
        return n;
    }

    __declspec(dllexport) void stop_capture() {
        g_running.store(false);
        if (g_thread.joinable()) g_thread.join();
        g_capture_client.Reset();
        g_audio_client.Reset();
    }
}