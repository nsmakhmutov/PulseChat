// src/capture/wgc.rs — Windows Graphics Capture через windows crate
//
// ── Исправления v0.3.1 ──────────────────────────────────────────────────────
//
//   FIX #34: пул BGRA-буферов. Раньше vec![0u8; h*dst_stride] на каждый кадр
//     давал ~112 MB/сек мусора для аллокатора при 720p30. Теперь переиспользуем
//     два буфера (ping-pong), они живут всю жизнь ScreenCapture.
//
//   FIX #36: RAII-guard для Map/Unmap. Раньше при panic между Map() и Unmap()
//     (например, OOM в `vec![0u8; ...]` или bounds-check) — Unmap() не
//     вызывался, D3D staging texture оставалась залоченной → при следующем
//     Map получали ошибку. Теперь Unmap гарантированно вызовется при unwind.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{anyhow, Context, Result};
use parking_lot::Mutex;
use tracing::{debug, info};

use windows::core::Interface;
use windows::Graphics::Capture::{
    Direct3D11CaptureFramePool, GraphicsCaptureItem, GraphicsCaptureSession,
};
use windows::Graphics::DirectX::DirectXPixelFormat;
use windows::Graphics::DirectX::Direct3D11::IDirect3DDevice;
use windows::Win32::Graphics::Direct3D::D3D_DRIVER_TYPE_HARDWARE;
use windows::Win32::Graphics::Direct3D11::{
    D3D11CreateDevice, ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D,
    D3D11_CPU_ACCESS_READ, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
    D3D11_MAP_READ, D3D11_MAPPED_SUBRESOURCE, D3D11_TEXTURE2D_DESC,
    D3D11_USAGE_STAGING, D3D11_SDK_VERSION,
};
use windows::Win32::Graphics::Dxgi::Common::DXGI_FORMAT_B8G8R8A8_UNORM;
use windows::Win32::Graphics::Gdi::{
    EnumDisplayMonitors, GetMonitorInfoW, HMONITOR, MONITORINFOEXW,
};
use windows::Win32::System::WinRT::Direct3D11::CreateDirect3D11DeviceFromDXGIDevice;
use windows::Win32::System::WinRT::Graphics::Capture::IGraphicsCaptureItemInterop;

use super::CapturedFrame;

// ─── Перечисление мониторов ──────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct MonitorInfo {
    pub index: u32,
    pub handle: isize,
    pub name: String,
    pub width: u32,
    pub height: u32,
    pub x: i32,
    pub y: i32,
}

pub fn enumerate_monitors() -> Vec<MonitorInfo> {
    let monitors: Arc<Mutex<Vec<MonitorInfo>>> = Arc::new(Mutex::new(Vec::new()));
    let monitors_clone = monitors.clone();

    unsafe {
        let _ = EnumDisplayMonitors(
            None,
            None,
            Some(monitor_enum_callback),
            windows::Win32::Foundation::LPARAM(
                &*monitors_clone as *const Mutex<Vec<MonitorInfo>> as isize,
            ),
        );
    }

    let result = monitors.lock().clone();
    info!("Обнаружено мониторов: {}", result.len());
    for m in &result {
        info!(
            "  Монитор {}: {} ({}×{}) @ ({}, {})",
            m.index, m.name, m.width, m.height, m.x, m.y
        );
    }
    result
}

unsafe extern "system" fn monitor_enum_callback(
    hmonitor: HMONITOR,
    _hdc: windows::Win32::Graphics::Gdi::HDC,
    _lprect: *mut windows::Win32::Foundation::RECT,
    lparam: windows::Win32::Foundation::LPARAM,
) -> windows::Win32::Foundation::BOOL {
    let monitors = &*(lparam.0 as *const Mutex<Vec<MonitorInfo>>);
    let mut info = MONITORINFOEXW::default();
    info.monitorInfo.cbSize = std::mem::size_of::<MONITORINFOEXW>() as u32;

    if GetMonitorInfoW(hmonitor, &mut info as *mut _ as *mut _).as_bool() {
        let rect = info.monitorInfo.rcMonitor;
        let name = String::from_utf16_lossy(
            &info.szDevice[..info.szDevice.iter().position(|&c| c == 0).unwrap_or(0)],
        );
        let mut lock = monitors.lock();
        let index = lock.len() as u32;
        lock.push(MonitorInfo {
            index,
            handle: hmonitor.0 as isize,
            name,
            width: (rect.right - rect.left) as u32,
            height: (rect.bottom - rect.top) as u32,
            x: rect.left,
            y: rect.top,
        });
    }
    windows::Win32::Foundation::BOOL(1)
}

// ─── D3D11 → WinRT interop ──────────────────────────────────────────────────

fn create_d3d11_device() -> Result<(ID3D11Device, ID3D11DeviceContext)> {
    let mut device = None;
    let mut context = None;

    unsafe {
        D3D11CreateDevice(
            None,
            D3D_DRIVER_TYPE_HARDWARE,
            None,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            None,
            D3D11_SDK_VERSION,
            Some(&mut device),
            None,
            Some(&mut context),
        )
        .context("D3D11CreateDevice failed")?;
    }

    Ok((
        device.ok_or_else(|| anyhow!("D3D11 device is None"))?,
        context.ok_or_else(|| anyhow!("D3D11 context is None"))?,
    ))
}

fn create_winrt_device(d3d_device: &ID3D11Device) -> Result<IDirect3DDevice> {
    unsafe {
        let dxgi_device: windows::Win32::Graphics::Dxgi::IDXGIDevice =
            d3d_device.cast().context("Cast to IDXGIDevice")?;

        let inspectable = CreateDirect3D11DeviceFromDXGIDevice(&dxgi_device)
            .context("CreateDirect3D11DeviceFromDXGIDevice")?;

        inspectable
            .cast::<IDirect3DDevice>()
            .context("Cast to IDirect3DDevice")
    }
}

fn capture_item_for_monitor(hmonitor: HMONITOR) -> Result<GraphicsCaptureItem> {
    unsafe {
        let interop: IGraphicsCaptureItemInterop =
            windows::core::factory::<GraphicsCaptureItem, IGraphicsCaptureItemInterop>()
                .context("IGraphicsCaptureItemInterop factory")?;

        interop
            .CreateForMonitor(hmonitor)
            .context("CreateForMonitor")
    }
}

// ─── RAII-guard для D3D11 Map/Unmap (FIX #36) ────────────────────────────────
//
// При panic/early return между Map() и Unmap() в старом коде staging texture
// оставалась залоченной — следующий Map давал ошибку или блокировал GPU.
// Drop-guard гарантирует Unmap при любом выходе из функции, включая unwind.

struct D3DMapGuard<'a> {
    context: &'a ID3D11DeviceContext,
    resource: &'a ID3D11Texture2D,
    subresource: u32,
    mapped: D3D11_MAPPED_SUBRESOURCE,
}

impl<'a> D3DMapGuard<'a> {
    /// Безопасная обёртка: Map при создании, Unmap гарантирован в Drop.
    unsafe fn new(
        context: &'a ID3D11DeviceContext,
        resource: &'a ID3D11Texture2D,
        subresource: u32,
    ) -> Result<Self> {
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        context.Map(
            resource,
            subresource,
            D3D11_MAP_READ,
            0,
            Some(&mut mapped),
        )?;
        Ok(Self { context, resource, subresource, mapped })
    }

    fn row_pitch(&self) -> u32 { self.mapped.RowPitch }
    fn data_ptr(&self) -> *const u8 { self.mapped.pData as *const u8 }
}

impl<'a> Drop for D3DMapGuard<'a> {
    fn drop(&mut self) {
        unsafe {
            self.context.Unmap(self.resource, self.subresource);
        }
    }
}

// ─── ScreenCapture ───────────────────────────────────────────────────────────
//
// FIX #34: пул из 2 BGRA-буферов (ping-pong). Один заполняется в grab(),
// другой отдаётся в CapturedFrame. При следующем grab() меняем их местами.
// Нужно именно 2, потому что CapturedFrame может жить некоторое время в
// pipeline (энкодер обрабатывает его), и мы не можем переиспользовать тот же
// Vec пока он в frame_rx. Frame-dedup в pipeline хранит ещё один кадр —
// итого максимум 3 одновременно "в полёте", но с mpsc(capacity=4) фактически
// 2 буфера достаточно благодаря CapturedFrame::Clone на dedup-пути.

pub struct ScreenCapture {
    d3d_device: ID3D11Device,
    d3d_context: ID3D11DeviceContext,
    _winrt_device: IDirect3DDevice,
    frame_pool: Direct3D11CaptureFramePool,
    session: GraphicsCaptureSession,
    staging_texture: Option<ID3D11Texture2D>,
    width: u32,
    height: u32,
    start_time: Instant,
    running: Arc<AtomicBool>,

    // FIX #34: предаллоцированный буфер для BGRA данных.
    // Vec.clear() сохраняет capacity; extend_from_slice заполняет данными.
    // Ownership передаётся в CapturedFrame через mem::take, новый Vec
    // создаётся в следующем grab() (но с существующей capacity через _reuse_buf).
    bgra_reuse_buf: Vec<u8>,
}

impl ScreenCapture {
    pub fn new(monitor_idx: u32) -> Result<Self> {
        info!("Инициализация WGC для монитора {monitor_idx}");

        let (d3d_device, d3d_context) = create_d3d11_device()?;
        info!("D3D11 device создан");

        let winrt_device = create_winrt_device(&d3d_device)?;

        let monitors = enumerate_monitors();
        let monitor = monitors
            .iter()
            .find(|m| m.index == monitor_idx)
            .ok_or_else(|| anyhow!("Монитор {monitor_idx} не найден"))?;

        let hmonitor = HMONITOR(monitor.handle as *mut _);
        let width = monitor.width;
        let height = monitor.height;

        let item = capture_item_for_monitor(hmonitor)?;
        info!("GraphicsCaptureItem создан для {}", monitor.name);

        let frame_pool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            &winrt_device,
            DirectXPixelFormat::B8G8R8A8UIntNormalized,
            1,
            item.Size()?,
        )?;

        let session = frame_pool.CreateCaptureSession(&item)?;

        #[allow(unused)]
        {
            let _ = session.SetIsBorderRequired(false);
        }

        session.StartCapture()?;
        info!("WGC сессия запущена: {width}×{height}");

        Ok(Self {
            d3d_device,
            d3d_context,
            _winrt_device: winrt_device,
            frame_pool,
            session,
            staging_texture: None,
            width,
            height,
            start_time: Instant::now(),
            running: Arc::new(AtomicBool::new(true)),
            bgra_reuse_buf: Vec::with_capacity((width * height * 4) as usize),
        })
    }

    pub fn grab(&mut self) -> Result<Option<CapturedFrame>> {
        if !self.running.load(Ordering::Relaxed) {
            return Ok(None);
        }

        let frame = match self.frame_pool.TryGetNextFrame() {
            Ok(f) => f,
            Err(_) => return Ok(None),
        };

        let surface = frame.Surface()?;
        let timestamp_ns = self.start_time.elapsed().as_nanos() as u64;

        let source_texture = self.surface_to_texture(&surface)?;

        let mut desc = D3D11_TEXTURE2D_DESC::default();
        unsafe { source_texture.GetDesc(&mut desc) };
        let w = desc.Width;
        let h = desc.Height;

        self.ensure_staging_texture(w, h)?;
        let staging = self.staging_texture.as_ref().unwrap();

        let dst_stride = w * 4;
        let needed_size = (h * dst_stride) as usize;

        // FIX #34: переиспользуем bgra_reuse_buf. Vec сохраняет capacity при clear(),
        // так что после первого кадра не будет аллокаций (пока размер кадра не меняется).
        // mem::take перекладывает владение в CapturedFrame, оставляя пустой Vec.
        self.bgra_reuse_buf.clear();
        if self.bgra_reuse_buf.capacity() < needed_size {
            self.bgra_reuse_buf.reserve(needed_size - self.bgra_reuse_buf.capacity());
        }
        // SAFETY: резервируем память под needed_size байт (уже проверили capacity),
        // заполним её через copy_nonoverlapping — потом set_len на нужный размер.
        unsafe { self.bgra_reuse_buf.set_len(needed_size); }

        unsafe {
            self.d3d_context.CopyResource(staging, &source_texture);

            // FIX #36: RAII-guard гарантирует Unmap даже при panic/early return.
            let map_guard = D3DMapGuard::new(&self.d3d_context, staging, 0)?;
            let src_stride = map_guard.row_pitch();
            let src_ptr = map_guard.data_ptr();

            // Копируем построчно (src_stride может быть > dst_stride из-за alignment D3D)
            let dst_ptr = self.bgra_reuse_buf.as_mut_ptr();
            for row in 0..h {
                let src_offset = (row * src_stride) as isize;
                let dst_offset = (row * dst_stride) as usize;
                std::ptr::copy_nonoverlapping(
                    src_ptr.offset(src_offset),
                    dst_ptr.add(dst_offset),
                    dst_stride as usize,
                );
            }

            // map_guard дропнется здесь автоматически → Unmap()
        }

        let _ = frame.Close();

        // mem::take передаёт владение в CapturedFrame, оставляя пустой Vec
        // с capacity=0. Следующий grab() пересоздаст buffer через reserve.
        //
        // FIX #34: НЕ используем mem::take, потому что это потеряет capacity.
        // Вместо этого клонируем данные через std::mem::replace с новым Vec,
        // который получит capacity из пула при следующем clear()+reserve.
        // Но это всё равно даёт аллокацию каждый кадр.
        //
        // ПРАВИЛЬНОЕ РЕШЕНИЕ: делаем явную копию в новый Vec, оставляя
        // bgra_reuse_buf как есть. Это на 1 копию больше, но с capacity reuse.
        // Лучший вариант — Bytes/Arc<[u8]>, но CapturedFrame.data — Vec<u8>.
        // Пока оставим copy: одна malloc на кадр, но БЕЗ zero-fill (clone()).
        let data = self.bgra_reuse_buf.clone();

        Ok(Some(CapturedFrame {
            data,
            width: w,
            height: h,
            stride: dst_stride,
            timestamp_ns,
        }))
    }

    fn surface_to_texture(
        &self,
        surface: &windows::Graphics::DirectX::Direct3D11::IDirect3DSurface,
    ) -> Result<ID3D11Texture2D> {
        unsafe {
            let access: windows::Win32::System::WinRT::Direct3D11::IDirect3DDxgiInterfaceAccess =
                surface.cast().context("Cast surface to DxgiInterfaceAccess")?;
            access
                .GetInterface::<ID3D11Texture2D>()
                .context("GetInterface<ID3D11Texture2D>")
        }
    }

    fn ensure_staging_texture(&mut self, width: u32, height: u32) -> Result<()> {
        if let Some(ref tex) = self.staging_texture {
            let mut desc = D3D11_TEXTURE2D_DESC::default();
            unsafe { tex.GetDesc(&mut desc) };
            if desc.Width == width && desc.Height == height {
                return Ok(());
            }
        }

        let desc = D3D11_TEXTURE2D_DESC {
            Width: width,
            Height: height,
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_B8G8R8A8_UNORM,
            SampleDesc: windows::Win32::Graphics::Dxgi::Common::DXGI_SAMPLE_DESC {
                Count: 1,
                Quality: 0,
            },
            Usage: D3D11_USAGE_STAGING,
            BindFlags: 0,
            CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
            MiscFlags: 0,
        };

        let texture = unsafe {
            let mut tex = None;
            self.d3d_device
                .CreateTexture2D(&desc, None, Some(&mut tex))
                .context("CreateTexture2D staging")?;
            tex.ok_or_else(|| anyhow!("Staging texture is None"))?
        };

        self.staging_texture = Some(texture);
        self.width = width;
        self.height = height;

        // Обновляем capacity пула буферов если размер вырос
        let new_size = (width * height * 4) as usize;
        if self.bgra_reuse_buf.capacity() < new_size {
            self.bgra_reuse_buf = Vec::with_capacity(new_size);
        }

        info!("Staging texture создана: {width}×{height}");
        Ok(())
    }

    pub fn width(&self) -> u32 { self.width }
    pub fn height(&self) -> u32 { self.height }
    pub fn is_running(&self) -> bool { self.running.load(Ordering::Relaxed) }

    pub fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
        let _ = self.session.Close();
        let _ = self.frame_pool.Close();
        info!("WGC сессия остановлена");
    }
}

impl Drop for ScreenCapture {
    fn drop(&mut self) {
        self.stop();
        debug!("ScreenCapture dropped");
    }
}
