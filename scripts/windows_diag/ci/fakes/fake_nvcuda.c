/*
 * Stand-in nvcuda.dll (CUDA driver API) for the Windows GPU decision matrix (ci_winmatrix). x64 only.
 *
 * Every export the installer's library probe calls, with the signatures it declares:
 *   base (828234f4a) install.ps1 Get-NvidiaLibraryProbeType, emitted P/Invoke, CallingConvention.Winapi
 *   head (71e4133bc) install.ps1 Read-NvidiaLibraryRawViaPython, ctypes.CDLL
 * On x64 there is one calling convention, so WINAPI (__stdcall) and cdecl callers agree.
 *
 *   CUresult cuInit(unsigned int flags)
 *   CUresult cuDriverGetVersion(int *version)
 *   CUresult cuDeviceGetCount(int *count)
 *   CUresult cuDeviceGet(CUdevice *device, int ordinal)          CUdevice is int
 *   CUresult cuDeviceGetAttribute(int *value, int attrib, CUdevice device)
 *            attrib 75 = COMPUTE_CAPABILITY_MAJOR, 76 = COMPUTE_CAPABILITY_MINOR
 * plus cuDeviceGetName, cuDeviceComputeCapability and cuDeviceTotalMem_v2.
 *
 * Environment: FAKE_CUDA (12.8 -> 12080), FAKE_CC (8.9), FAKE_GPU_COUNT (1),
 * FAKE_GPU_NAME, FAKE_LOG (one line per call).
 */
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __declspec(dllexport)
#define MAX_GPUS 16

#define CUDA_SUCCESS 0
#define CUDA_ERROR_INVALID_VALUE 1
#define CUDA_ERROR_NOT_INITIALIZED 3
#define CUDA_ERROR_NO_DEVICE 100
#define CUDA_ERROR_INVALID_DEVICE 101

#define ATTR_CC_MAJOR 75
#define ATTR_CC_MINOR 76

static int g_initialized = 0;

static void fake_log(const char *fmt, ...)
{
    char path[1024], exe[MAX_PATH], line[512];
    const char *base;
    DWORD n;
    FILE *f;
    va_list ap;
    n = GetEnvironmentVariableA("FAKE_LOG", path, sizeof(path));
    if (n == 0 || n >= sizeof(path)) return;
    exe[0] = '\0';
    GetModuleFileNameA(NULL, exe, sizeof(exe));
    exe[sizeof(exe) - 1] = '\0';
    base = strrchr(exe, '\\');
    base = base ? base + 1 : exe;
    va_start(ap, fmt);
    _vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    line[sizeof(line) - 1] = '\0';
    f = fopen(path, "a");
    if (!f) return;
    fprintf(f, "nvcuda host=%s pid=%lu %s\n", base, (unsigned long)GetCurrentProcessId(), line);
    fclose(f);
}

static const char *env_or(const char *name, const char *fallback, char *buf, DWORD len)
{
    DWORD n = GetEnvironmentVariableA(name, buf, len);
    if (n == 0 || n >= len) return fallback;
    return buf;
}

static int gpu_count(void)
{
    char buf[32];
    int n = atoi(env_or("FAKE_GPU_COUNT", "1", buf, sizeof(buf)));
    if (n < 0) n = 0;
    if (n > MAX_GPUS) n = MAX_GPUS;
    return n;
}

static int cuda_packed(void)
{
    char buf[32];
    const char *v = env_or("FAKE_CUDA", "12.8", buf, sizeof(buf));
    int major = 0, minor = 0;
    if (sscanf(v, "%d.%d", &major, &minor) < 1) return 0;
    return major * 1000 + minor * 10;
}

static void compute_cap(int *major, int *minor)
{
    char buf[32];
    const char *v = env_or("FAKE_CC", "8.9", buf, sizeof(buf));
    *major = 0; *minor = 0;
    sscanf(v, "%d.%d", major, minor);
}

EXPORT int WINAPI cuInit(unsigned int flags)
{
    fake_log("cuInit(%u)", flags);
    if (flags != 0) return CUDA_ERROR_INVALID_VALUE;
    if (gpu_count() == 0) return CUDA_ERROR_NO_DEVICE;
    g_initialized = 1;
    return CUDA_SUCCESS;
}

EXPORT int WINAPI cuDriverGetVersion(int *version)
{
    int v = cuda_packed();
    fake_log("cuDriverGetVersion() -> %d", v);
    if (!version) return CUDA_ERROR_INVALID_VALUE;
    *version = v;
    return CUDA_SUCCESS;
}

EXPORT int WINAPI cuDeviceGetCount(int *count)
{
    int n = gpu_count();
    fake_log("cuDeviceGetCount() -> %d", n);
    if (!g_initialized) return CUDA_ERROR_NOT_INITIALIZED;
    if (!count) return CUDA_ERROR_INVALID_VALUE;
    *count = n;
    return CUDA_SUCCESS;
}

EXPORT int WINAPI cuDeviceGet(int *device, int ordinal)
{
    fake_log("cuDeviceGet(%d)", ordinal);
    if (!g_initialized) return CUDA_ERROR_NOT_INITIALIZED;
    if (!device) return CUDA_ERROR_INVALID_VALUE;
    if (ordinal < 0 || ordinal >= gpu_count()) return CUDA_ERROR_INVALID_DEVICE;
    *device = ordinal;
    return CUDA_SUCCESS;
}

EXPORT int WINAPI cuDeviceGetAttribute(int *value, int attrib, int device)
{
    int ma, mi;
    compute_cap(&ma, &mi);
    fake_log("cuDeviceGetAttribute(attrib=%d, device=%d)", attrib, device);
    if (!g_initialized) return CUDA_ERROR_NOT_INITIALIZED;
    if (!value) return CUDA_ERROR_INVALID_VALUE;
    if (device < 0 || device >= gpu_count()) return CUDA_ERROR_INVALID_DEVICE;
    if (attrib == ATTR_CC_MAJOR) { *value = ma; return CUDA_SUCCESS; }
    if (attrib == ATTR_CC_MINOR) { *value = mi; return CUDA_SUCCESS; }
    return CUDA_ERROR_INVALID_VALUE;
}

EXPORT int WINAPI cuDeviceComputeCapability(int *major, int *minor, int device)
{
    fake_log("cuDeviceComputeCapability(device=%d)", device);
    if (!g_initialized) return CUDA_ERROR_NOT_INITIALIZED;
    if (!major || !minor) return CUDA_ERROR_INVALID_VALUE;
    if (device < 0 || device >= gpu_count()) return CUDA_ERROR_INVALID_DEVICE;
    compute_cap(major, minor);
    return CUDA_SUCCESS;
}

EXPORT int WINAPI cuDeviceGetName(char *name, int length, int device)
{
    char buf[128];
    const char *v = env_or("FAKE_GPU_NAME", "NVIDIA GeForce RTX 4090", buf, sizeof(buf));
    fake_log("cuDeviceGetName(device=%d)", device);
    if (!g_initialized) return CUDA_ERROR_NOT_INITIALIZED;
    if (!name || length <= 0) return CUDA_ERROR_INVALID_VALUE;
    if (device < 0 || device >= gpu_count()) return CUDA_ERROR_INVALID_DEVICE;
    strncpy(name, v, (size_t)length - 1);
    name[length - 1] = '\0';
    return CUDA_SUCCESS;
}

EXPORT int WINAPI cuDeviceTotalMem_v2(size_t *bytes, int device)
{
    fake_log("cuDeviceTotalMem_v2(device=%d)", device);
    if (!g_initialized) return CUDA_ERROR_NOT_INITIALIZED;
    if (!bytes) return CUDA_ERROR_INVALID_VALUE;
    if (device < 0 || device >= gpu_count()) return CUDA_ERROR_INVALID_DEVICE;
    *bytes = (size_t)24564 * 1024 * 1024;
    return CUDA_SUCCESS;
}
