// src/capture/wgc.rs — Windows Graphics Capture через windows crate
//
// Использует Direct3D 11 + Windows.Graphics.Capture для захвата монитора.
// Каждый вызов `grab()` возвращает CapturedFrame с BGRA данными.

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
// windows 0.58: CreateDirect3D11DeviceFromDXGIDevice and IDirect3DDxgiInterfaceAccess
// are in Win32::System::WinRT::Direct3D11 (requires feature Win32_System_WinRT_Direct3D11)
use windows::Win32::System::WinRT::Direct3D11::CreateDirect3D11DeviceFromDXGIDevice;
use windows::Win32::System::WinRT::Graphics::Capture::IGraphicsCaptureItemInterop;

use super::CapturedFrame;

// ─── Перечисление мониторов ──────────────────────────────────────────────────

/// Информация о мониторе
#[derive(Debug, Clone)]
pub struct MonitorInfo {
    pub index: u32,
    pub handle: isize, // HMONITOR as isize
    pub name: String,
    pub width: u32,
    pub height: u32,
    pub x: i32,
    pub y: i32,
}

/// Перечисляет все подключённые мониторы через Win32 GDI.
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

// ─── ScreenCapture ───────────────────────────────────────────────────────────

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

        unsafe {
            self.d3d_context.CopyResource(staging, &source_texture);

            // windows 0.58: Map() takes 5 args, mapped data written to out param
            let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
            self.d3d_context.Map(
                staging,
                0,
                D3D11_MAP_READ,
                0,
                Some(&mut mapped),
            )?;

            let src_stride = mapped.RowPitch;
            let dst_stride = w * 4;
            let mut data = vec![0u8; (h * dst_stride) as usize];

            let src_ptr = mapped.pData as *const u8;
            for row in 0..h {
                let src_offset = (row * src_stride) as isize;
                let dst_offset = (row * dst_stride) as usize;
                std::ptr::copy_nonoverlapping(
                    src_ptr.offset(src_offset),
                    data[dst_offset..].as_mut_ptr(),
                    dst_stride as usize,
                );
            }

            self.d3d_context.Unmap(staging, 0);
            frame.Close()?;

            Ok(Some(CapturedFrame {
                data,
                width: w,
                height: h,
                stride: dst_stride,
                timestamp_ns,
            }))
        }
    }

    fn surface_to_texture(
        &self,
        surface: &windows::Graphics::DirectX::Direct3D11::IDirect3DSurface,
    ) -> Result<ID3D11Texture2D> {
        unsafe {
            // windows 0.58: IDirect3DDxgiInterfaceAccess is in WinRT::Direct3D11
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

        // windows 0.58: BindFlags/CPUAccessFlags/MiscFlags are u32, not flag types
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
