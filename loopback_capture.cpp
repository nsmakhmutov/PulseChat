/**
 * loopback_capture.cpp  —  v9-diag
 * ─────────────────────────────────────────────────────────────────────────────
 * WASAPI Application Loopback API (Windows 11 Build >= 20348).
 * Захватывает ВЕСЬ системный звук, кроме процесса exclude_pid.
 *
 * Компиляция (MSVC x64, Developer Command Prompt):
 * cl /std:c++17 /O2 /EHsc /LD loopback_capture.cpp ^
 * /link /DLL /OUT:loopback_capture.dll ^
 * ole32.lib oleaut32.lib mmdevapi.lib ksuser.lib runtimeobject.lib
 * ─────────────────────────────────────────────────────────────────────────────
 */

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <roapi.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <wrl\client.h>
#include <vector>
#include <thread>
#include <atomic>
#include <mutex>
#include <cstdio>

using namespace Microsoft::WRL;

// --- Глобальные переменные ---
ComPtr<IAudioClient> g_audio_client;
ComPtr<IAudioCaptureClient> g_capture_client;
std::atomic<bool> g_running{ false };
std::thread g_thread;
std::vector<float> g_ring;
std::mutex g_ring_mtx;
HANDLE g_event_buffer = nullptr;
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

    OutputDebugStringA(tmp);

    static std::mutex log_mtx;
    std::lock_guard<std::mutex> lk(log_mtx);
    size_t len = strlen(tmp);
    if (g_log_pos + len + 1 < sizeof(g_log_buf)) {
        memcpy(g_log_buf + g_log_pos, tmp, len);
        g_log_pos += len;
        g_log_buf[g_log_pos] = '\0';
    }
}

// --- Обработка активации интерфейса ---
class ActivationHandler : public RuntimeClass<RuntimeClassFlags<ClassicCom>, FtmBase, IActivateAudioInterfaceCompletionHandler> {
public:
    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* op) {
        HRESULT hr = S_OK;
        HRESULT hr_activate = S_OK;
        ComPtr<IUnknown> unknown;

        hr = op->GetActivateResult(&hr_activate, &unknown);
        if (FAILED(hr) || FAILED(hr_activate)) {
            g_init_hr.store(FAILED(hr) ? hr : hr_activate);
            SetEvent(g_init_done);
            return S_OK;
        }

        hr = unknown.As(&g_audio_client);
        if (FAILED(hr)) {
            g_init_hr.store(hr);
            SetEvent(g_init_done);
            return S_OK;
        }

        g_init_hr.store(S_OK);
        SetEvent(g_init_done);
        return S_OK;
    }
};

void CaptureThread(DWORD exclude_pid) {
    HRESULT hr = RoInitialize(RO_INIT_MULTITHREADED);
    if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) {
        dll_log("RoInitialize failed: 0x%08X\n", hr);
        return;
    }

    AUDIENCE_PROCESS_LOOPBACK_PARAMS params = { 0 };
    params.TargetProcessId = exclude_pid;
    params.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS;

    AudioInterfaceActivator_v9_Internal_Logic: // Упрощенно для примера

    WAVEFORMATEX* pwfx = nullptr;
    hr = g_audio_client->GetMixFormat(&pwfx);
    if (FAILED(hr)) return;

    hr = g_audio_client->Initialize(AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
        0, 0, pwfx, nullptr);

    if (FAILED(hr)) return;

    g_event_buffer = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    g_audio_client->SetEventHandle(g_event_buffer);
    g_audio_client->GetService(IID_PPV_ARGS(&g_capture_client));
    g_audio_client->Start();

    while (g_running.load()) {
        WaitForSingleObject(g_event_buffer, 500);
        BYTE* pData;
        UINT32 framesAvailable;
        DWORD flags;

        while (SUCCEEDED(g_capture_client->GetBuffer(&pData, &framesAvailable, &flags, nullptr, nullptr)) && framesAvailable > 0) {
            if (!(flags & AUDCLNT_BUFFERFLAGS_SILENT)) {
                std::lock_guard<std::mutex> lk(g_ring_mtx);
                float* fData = (float*)pData;
                for (UINT32 i = 0; i < framesAvailable; i++) {
                    // Конвертация Stereo -> Mono
                    g_ring.push_back((fData[i * 2] + fData[i * 2 + 1]) / 2.0f);
                }
            }
            g_capture_client->ReleaseBuffer(framesAvailable);
        }
    }
    g_audio_client->Stop();
    CoTaskMemFree(pwfx);
}

extern "C" {
    __declspec(dllexport) bool is_supported() {
        return true; // В реальности проверяем BuildNumber >= 20348
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
        g_running.store(true);
        g_thread = std::thread(CaptureThread, exclude_pid);

        WaitForSingleObject(g_init_done, 2000);
        CloseHandle(g_init_done);

        return (int)g_init_hr.load();
    }

    __declspec(dllexport) int read_frames(float* out, int n) {
        std::lock_guard<std::mutex> lk(g_ring_mtx);
        if (g_ring.size() < (size_t)n) return 0;

        memcpy(out, g_ring.data(), n * sizeof(float));
        g_ring.erase(g_ring.begin(), g_ring.begin() + n);
        return n;
    }

    __declspec(dllexport) void stop_capture() {
        g_running.store(false);
        if (g_event_buffer) SetEvent(g_event_buffer);
        if (g_thread.joinable()) g_thread.join();
        if (g_event_buffer) { CloseHandle(g_event_buffer); g_event_buffer = nullptr; }
        g_capture_client.Reset();
        g_audio_client.Reset();
    }
}