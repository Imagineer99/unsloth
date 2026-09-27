/*
 * Stand-in nvml.dll for the Windows GPU decision matrix (ci_winmatrix). x64 only.
 *
 * Every export the installer's library probe calls, with the signatures it declares:
 *   base (828234f4a) install.ps1 Get-NvidiaLibraryProbeType, emitted P/Invoke, CallingConvention.Winapi
 *   head (71e4133bc) install.ps1 Read-NvidiaLibraryRawViaPython, ctypes.CDLL
 * On x64 there is one calling convention, so WINAPI (__stdcall) and cdecl callers agree.
 *
 *   int nvmlInit_v2(void)
 *   int nvmlShutdown(void)
 *   int nvmlSystemGetCudaDriverVersion_v2(int *version)
 *   int nvmlDeviceGetCount_v2(unsigned int *count)
 *   int nvmlDeviceGetHandleByIndex_v2(unsigned int index, nvmlDevice_t *device)
 *   int nvmlDeviceGetCudaComputeCapability(nvmlDevice_t device, int *major, int *minor)
 * plus nvmlInit, nvmlSystemGetCudaDriverVersion, nvmlDeviceGetCount, nvmlDeviceGetHandleByIndex,
 * nvmlSystemGetDriverVersion, nvmlDeviceGetName, nvmlDeviceGetNvLinkState,
 * nvmlDeviceGetP2PStatus and nvmlErrorString for other readers (studio/nvidia_probe.py).
 *
 * Environment: FAKE_CUDA (12.8 -> 12080), FAKE_CC (8.9), FAKE_GPU_COUNT (1),
 * FAKE_GPU_NAME, FAKE_NVML_HANG=1 (nvmlInit sleeps 600 s), FAKE_LOG (one line per call).
 */
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __declspec(dllexport)
#define MAX_GPUS 16

#define NVML_SUCCESS 0
#define NVML_ERROR_UNINITIALIZED 1
#define NVML_ERROR_INVALID_ARGUMENT 2
#define NVML_ERROR_NOT_SUPPORTED 3
#define NVML_ERROR_INSUFFICIENT_SIZE 7

typedef struct fake_device { unsigned int magic; unsigned int index; } fake_device;
typedef fake_device *nvmlDevice_t;

static fake_device g_devices[MAX_GPUS];
static int g_init_count = 0;

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
    fprintf(f, "nvml host=%s pid=%lu %s\n", base, (unsigned long)GetCurrentProcessId(), line);
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

/* "12.8" -> 12080, "13.0" -> 13000: major * 1000 + minor * 10, as the driver reports it. */
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

static int valid_device(nvmlDevice_t d)
{
    /* A handle truncated to 32 bits by a caller lands outside the table and is refused. */
    if (d < &g_devices[0] || d >= &g_devices[MAX_GPUS]) return 0;
    return d->magic == 0x4e564d4cu && (int)d->index < gpu_count();
}

EXPORT int WINAPI nvmlInit_v2(void)
{
    char buf[8];
    int i;
    fake_log("nvmlInit_v2()");
    if (strcmp(env_or("FAKE_NVML_HANG", "0", buf, sizeof(buf)), "1") == 0) {
        fake_log("nvmlInit_v2 hanging 600 s (FAKE_NVML_HANG=1)");
        Sleep(600000);
    }
    for (i = 0; i < MAX_GPUS; i++) { g_devices[i].magic = 0x4e564d4cu; g_devices[i].index = (unsigned int)i; }
    g_init_count++;
    return NVML_SUCCESS;
}

EXPORT int WINAPI nvmlInit(void)
{
    fake_log("nvmlInit() -> nvmlInit_v2");
    return nvmlInit_v2();
}

EXPORT int WINAPI nvmlShutdown(void)
{
    fake_log("nvmlShutdown()");
    if (g_init_count <= 0) return NVML_ERROR_UNINITIALIZED;
    g_init_count--;
    return NVML_SUCCESS;
}

EXPORT int WINAPI nvmlSystemGetCudaDriverVersion_v2(int *version)
{
    int v = cuda_packed();
    fake_log("nvmlSystemGetCudaDriverVersion_v2() -> %d", v);
    if (!version) return NVML_ERROR_INVALID_ARGUMENT;
    *version = v;
    return NVML_SUCCESS;
}

EXPORT int WINAPI nvmlSystemGetCudaDriverVersion(int *version)
{
    return nvmlSystemGetCudaDriverVersion_v2(version);
}

EXPORT int WINAPI nvmlSystemGetDriverVersion(char *version, unsigned int length)
{
    fake_log("nvmlSystemGetDriverVersion(len=%u)", length);
    if (!version) return NVML_ERROR_INVALID_ARGUMENT;
    if (length < 7) return NVML_ERROR_INSUFFICIENT_SIZE;
    strcpy(version, "572.83");
    return NVML_SUCCESS;
}

EXPORT int WINAPI nvmlDeviceGetCount_v2(unsigned int *count)
{
    int n = gpu_count();
    fake_log("nvmlDeviceGetCount_v2() -> %d", n);
    if (g_init_count <= 0) return NVML_ERROR_UNINITIALIZED;
    if (!count) return NVML_ERROR_INVALID_ARGUMENT;
    *count = (unsigned int)n;
    return NVML_SUCCESS;
}

EXPORT int WINAPI nvmlDeviceGetCount(unsigned int *count)
{
    return nvmlDeviceGetCount_v2(count);
}

EXPORT int WINAPI nvmlDeviceGetHandleByIndex_v2(unsigned int index, nvmlDevice_t *device)
{
    fake_log("nvmlDeviceGetHandleByIndex_v2(%u)", index);
    if (g_init_count <= 0) return NVML_ERROR_UNINITIALIZED;
    if (!device || index >= (unsigned int)gpu_count()) return NVML_ERROR_INVALID_ARGUMENT;
    *device = &g_devices[index];
    return NVML_SUCCESS;
}

EXPORT int WINAPI nvmlDeviceGetHandleByIndex(unsigned int index, nvmlDevice_t *device)
{
    return nvmlDeviceGetHandleByIndex_v2(index, device);
}

EXPORT int WINAPI nvmlDeviceGetCudaComputeCapability(nvmlDevice_t device, int *major, int *minor)
{
    int ma, mi;
    compute_cap(&ma, &mi);
    fake_log("nvmlDeviceGetCudaComputeCapability(handle=%p) -> %d.%d", (void *)device, ma, mi);
    if (g_init_count <= 0) return NVML_ERROR_UNINITIALIZED;
    if (!major || !minor || !valid_device(device)) return NVML_ERROR_INVALID_ARGUMENT;
    *major = ma;
    *minor = mi;
    return NVML_SUCCESS;
}

EXPORT int WINAPI nvmlDeviceGetName(nvmlDevice_t device, char *name, unsigned int length)
{
    char buf[128];
    const char *v = env_or("FAKE_GPU_NAME", "NVIDIA GeForce RTX 4090", buf, sizeof(buf));
    fake_log("nvmlDeviceGetName(handle=%p)", (void *)device);
    if (g_init_count <= 0) return NVML_ERROR_UNINITIALIZED;
    if (!name || !valid_device(device)) return NVML_ERROR_INVALID_ARGUMENT;
    if (length <= strlen(v)) return NVML_ERROR_INSUFFICIENT_SIZE;
    strcpy(name, v);
    return NVML_SUCCESS;
}

EXPORT int WINAPI nvmlDeviceGetNvLinkState(nvmlDevice_t device, unsigned int link, int *isActive)
{
    fake_log("nvmlDeviceGetNvLinkState(handle=%p, link=%u)", (void *)device, link);
    (void)isActive;
    return NVML_ERROR_NOT_SUPPORTED;
}

EXPORT int WINAPI nvmlDeviceGetP2PStatus(nvmlDevice_t device1, nvmlDevice_t device2, int p2pIndex, int *p2pStatus)
{
    fake_log("nvmlDeviceGetP2PStatus(%p, %p, %d)", (void *)device1, (void *)device2, p2pIndex);
    (void)p2pStatus;
    return NVML_ERROR_NOT_SUPPORTED;
}

EXPORT const char *WINAPI nvmlErrorString(int result)
{
    switch (result) {
    case NVML_SUCCESS: return "Success";
    case NVML_ERROR_UNINITIALIZED: return "Uninitialized";
    case NVML_ERROR_INVALID_ARGUMENT: return "Invalid Argument";
    case NVML_ERROR_NOT_SUPPORTED: return "Not Supported";
    case NVML_ERROR_INSUFFICIENT_SIZE: return "Insufficient Size";
    default: return "Unknown Error";
    }
}
