Package: amd-amf:x64-windows-static@1.4.36

**Host Environment**

- Host: x64-windows
- Compiler: MSVC 19.50.35727.0
- CMake Version: 4.3.0
-    vcpkg-tool version: 2026-03-04-4b3e4c276b5b87a649e66341e11553e8c577459c
    vcpkg-scripts version: 67f167b2f7 2026-03-25 (9 hours ago)

**To Reproduce**

`vcpkg install `

**Failure logs**

```
Downloading https://github.com/GPUOpen-LibrariesAndSDKs/AMF/archive/v1.4.36.tar.gz -> GPUOpen-LibrariesAndSDKs-AMF-v1.4.36.tar.gz
GPUOpen-LibrariesAndSDKs-AMF-v1.4.36.tar.gz.2168.part: error: download from https://github.com/GPUOpen-LibrariesAndSDKs/AMF/archive/v1.4.36.tar.gz had an unexpected hash
note: Expected: 589fccabaadb27e48e9adb1d3594db2adadee343c966f8db99ff29a92ec78ae6b0c42f13113a4fc66da0044ee660cfa1caf6867c508af044935646c09f5af50e
note: Actual  : e19f8f98448412812ea1a4bf677ea501bebfc37871160e1cd0d0d2bf91af22f2115406949b594f405dab153952dcc3cbdc666ef2e6be1b768b803cdde7e23a7b
CMake Error at scripts/cmake/vcpkg_download_distfile.cmake:136 (message):
  Download failed, halting portfile.
Call Stack (most recent call first):
  scripts/cmake/vcpkg_from_github.cmake:120 (vcpkg_download_distfile)
  buildtrees/versioning_/versions/amd-amf/bd224304fd2caeb6f476511884069744e4b88f8f/portfile.cmake:1 (vcpkg_from_github)
  scripts/ports.cmake:206 (include)



```

**Additional context**

<details><summary>vcpkg.json</summary>

```
{
  "name": "inpulse-media-engine",
  "version": "0.1.0",
  "builtin-baseline": "4de314563a5b383e29e71778335876adcbad77de",
  "overrides": [
    {
      "name": "ffmpeg",
      "version": "7.1.1"
    }
  ],
  "dependencies": [
    {
      "name": "ffmpeg",
      "features": [
        "avcodec",
        "avformat",
        "avfilter",
        "swscale",
        "swresample",
        "nvcodec",
        "amf"
      ]
    }
  ]
}

```
</details>
