/*
 * Stand-in nvidia-smi for the Windows GPU decision matrix (ci_winmatrix).
 *
 * Output contract, taken from the call sites that parse it:
 *   install.ps1 / studio/setup.ps1  Test-NvidiaSmiHasGpu:  -L, exit 0, '(?m)^GPU\s+\d+:'
 *   install.ps1 / studio/setup.ps1  banner:  'CUDA(?: UMD)? Version:\s+(\d+)\.(\d+)'
 *   install.ps1 / studio/setup.ps1  --query-gpu=compute_cap --format=csv,noheader[,nounits]
 *   install.ps1 / studio/setup.ps1  --query-gpu=name,compute_cap,driver_version --format=csv,noheader
 *   install.ps1 / studio/setup.ps1  --query-gpu=uuid --format=csv,noheader
 *   studio/install_python_stack.py  -L ("GPU " in stdout), --query-gpu=compute_cap
 *   studio/install_llama_prebuilt.py -L, bare banner, --query-gpu=index,uuid,compute_cap
 *
 * Environment:
 *   FAKE_CUDA       CUDA version printed in the banner (default 12.8)
 *   FAKE_CC         compute capability of every GPU (default 8.9)
 *   FAKE_GPU_COUNT  number of GPUs (default 1; 0 behaves like a driver with no device)
 *   FAKE_GPU_NAME   marketing name (default NVIDIA GeForce RTX 4090)
 *   FAKE_LOG        when set, one line per invocation is appended to this file
 */
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_GPUS 16
#define MAX_FIELDS 32

static const char *DRIVER_VERSION = "572.83";

static const char *env_or(const char *name, const char *fallback)
{
    const char *v = getenv(name);
    return (v && v[0]) ? v : fallback;
}

static int gpu_count(void)
{
    const char *v = getenv("FAKE_GPU_COUNT");
    int n;
    if (!v || !v[0]) return 1;
    n = atoi(v);
    if (n < 0) n = 0;
    if (n > MAX_GPUS) n = MAX_GPUS;
    return n;
}

static void gpu_uuid(int index, char *buf, size_t len)
{
    if (index == 0)
        _snprintf(buf, len, "GPU-11111111-2222-3333-4444-555555555555");
    else
        _snprintf(buf, len, "GPU-11111111-2222-3333-4444-%012d", index);
    buf[len - 1] = '\0';
}

static void log_call(int argc, char **argv)
{
    const char *path = getenv("FAKE_LOG");
    FILE *f;
    int i;
    if (!path || !path[0]) return;
    f = fopen(path, "a");
    if (!f) return;
    fprintf(f, "nvidia-smi pid=%lu args=[", (unsigned long)GetCurrentProcessId());
    for (i = 1; i < argc; i++) fprintf(f, "%s%s", i > 1 ? " " : "", argv[i]);
    fprintf(f, "]\n");
    fclose(f);
}

static void print_list(int n)
{
    int i;
    char uuid[64];
    const char *name = env_or("FAKE_GPU_NAME", "NVIDIA GeForce RTX 4090");
    for (i = 0; i < n; i++) {
        gpu_uuid(i, uuid, sizeof(uuid));
        printf("GPU %d: %s (UUID: %s)\n", i, name, uuid);
    }
}

static void print_banner(int n)
{
    int i;
    const char *name = env_or("FAKE_GPU_NAME", "NVIDIA GeForce RTX 4090");
    const char *cuda = env_or("FAKE_CUDA", "12.8");
    printf("Fri Sep 26 10:00:00 2026\n");
    printf("+-----------------------------------------------------------------------------------------+\n");
    printf("| NVIDIA-SMI %s       Driver Version: %s       CUDA Version: %s     |\n",
           DRIVER_VERSION, DRIVER_VERSION, cuda);
    printf("|-----------------------------------------+------------------------+----------------------+\n");
    printf("| GPU  Name                  Driver-Model | Bus-Id          Disp.A | Volatile Uncorr. ECC |\n");
    printf("| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |\n");
    printf("|                                         |                        |               MIG M. |\n");
    printf("|=========================================+========================+======================|\n");
    for (i = 0; i < n; i++) {
        printf("| %3d  %-24.24s      WDDM  |   00000000:%02X:00.0  On |                  Off |\n", i, name, i + 1);
        printf("|  0%%   38C    P8             21W /  450W |    1024MiB /  24564MiB |      2%%      Default |\n");
        printf("|                                         |                        |                  N/A |\n");
        printf("+-----------------------------------------+------------------------+----------------------+\n");
    }
    printf("\n");
    printf("+-----------------------------------------------------------------------------------------+\n");
    printf("| Processes:                                                                              |\n");
    printf("|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |\n");
    printf("|        ID   ID                                                               Usage      |\n");
    printf("|=========================================================================================|\n");
    printf("|  No running processes found                                                             |\n");
    printf("+-----------------------------------------------------------------------------------------+\n");
}

/* Header text and value of one field. unit is "" for unitless fields. */
static int field_value(const char *field, int index, char *out, size_t len, const char **unit)
{
    char uuid[64];
    *unit = "";
    if (strcmp(field, "name") == 0 || strcmp(field, "gpu_name") == 0) {
        _snprintf(out, len, "%s", env_or("FAKE_GPU_NAME", "NVIDIA GeForce RTX 4090"));
    } else if (strcmp(field, "compute_cap") == 0) {
        _snprintf(out, len, "%s", env_or("FAKE_CC", "8.9"));
    } else if (strcmp(field, "driver_version") == 0) {
        _snprintf(out, len, "%s", DRIVER_VERSION);
    } else if (strcmp(field, "index") == 0) {
        _snprintf(out, len, "%d", index);
    } else if (strcmp(field, "uuid") == 0 || strcmp(field, "gpu_uuid") == 0) {
        gpu_uuid(index, uuid, sizeof(uuid));
        _snprintf(out, len, "%s", uuid);
    } else if (strcmp(field, "memory.total") == 0) {
        _snprintf(out, len, "24564"); *unit = "MiB";
    } else if (strcmp(field, "memory.used") == 0) {
        _snprintf(out, len, "1024"); *unit = "MiB";
    } else if (strcmp(field, "memory.free") == 0) {
        _snprintf(out, len, "23540"); *unit = "MiB";
    } else if (strcmp(field, "pci.bus_id") == 0) {
        _snprintf(out, len, "00000000:%02X:00.0", index + 1);
    } else if (strcmp(field, "utilization.gpu") == 0) {
        _snprintf(out, len, "2"); *unit = "%";
    } else if (strcmp(field, "temperature.gpu") == 0) {
        _snprintf(out, len, "38");
    } else {
        return 0;
    }
    out[len - 1] = '\0';
    return 1;
}

static int split_csv(char *s, char **items, int max)
{
    int n = 0;
    char *tok = strtok(s, ",");
    while (tok && n < max) {
        while (*tok == ' ') tok++;
        if (*tok) items[n++] = tok;
        tok = strtok(NULL, ",");
    }
    return n;
}

static int run_query(const char *query, const char *format, const char *ids, int n)
{
    char qbuf[1024], fbuf[256], ibuf[256], value[256];
    char *fields[MAX_FIELDS], *fmt[8], *idv[MAX_GPUS];
    int nf, nfmt, nid, i, j, k, noheader = 0, nounits = 0, selected[MAX_GPUS];
    const char *unit;

    strncpy(qbuf, query, sizeof(qbuf) - 1); qbuf[sizeof(qbuf) - 1] = '\0';
    nf = split_csv(qbuf, fields, MAX_FIELDS);
    if (nf == 0) {
        fprintf(stderr, "Missing value for --query-gpu.\n");
        return 2;
    }
    for (j = 0; j < nf; j++) {
        if (!field_value(fields[j], 0, value, sizeof(value), &unit)) {
            fprintf(stderr, "Field \"%s\" is not a valid field to query.\n", fields[j]);
            return 2;
        }
    }
    if (format) {
        strncpy(fbuf, format, sizeof(fbuf) - 1); fbuf[sizeof(fbuf) - 1] = '\0';
        nfmt = split_csv(fbuf, fmt, 8);
        for (k = 0; k < nfmt; k++) {
            if (strcmp(fmt[k], "noheader") == 0) noheader = 1;
            else if (strcmp(fmt[k], "nounits") == 0) nounits = 1;
            else if (strcmp(fmt[k], "csv") != 0) {
                fprintf(stderr, "\"%s\" is not a valid format option.\n", fmt[k]);
                return 2;
            }
        }
    }
    for (i = 0; i < n; i++) selected[i] = (ids == NULL);
    if (ids) {
        strncpy(ibuf, ids, sizeof(ibuf) - 1); ibuf[sizeof(ibuf) - 1] = '\0';
        nid = split_csv(ibuf, idv, MAX_GPUS);
        for (k = 0; k < nid; k++) {
            int want = atoi(idv[k]);
            if (want < 0 || want >= n) {
                fprintf(stderr, "No devices were found\n");
                return 6;
            }
            selected[want] = 1;
        }
    }
    if (!noheader) {
        for (j = 0; j < nf; j++) {
            field_value(fields[j], 0, value, sizeof(value), &unit);
            if (!nounits && unit[0]) printf("%s%s [%s]", j ? ", " : "", fields[j], unit);
            else printf("%s%s", j ? ", " : "", fields[j]);
        }
        printf("\n");
    }
    for (i = 0; i < n; i++) {
        if (!selected[i]) continue;
        for (j = 0; j < nf; j++) {
            field_value(fields[j], i, value, sizeof(value), &unit);
            if (!nounits && unit[0]) {
                if (strcmp(unit, "%") == 0) printf("%s%s %%", j ? ", " : "", value);
                else printf("%s%s %s", j ? ", " : "", value, unit);
            } else {
                printf("%s%s", j ? ", " : "", value);
            }
        }
        printf("\n");
    }
    return 0;
}

static const char *opt_value(int argc, char **argv, int *i, const char *longname, const char *shortname)
{
    size_t len = strlen(longname);
    const char *a = argv[*i];
    if (strncmp(a, longname, len) == 0 && a[len] == '=') return a + len + 1;
    if (strcmp(a, longname) == 0 || (shortname && strcmp(a, shortname) == 0)) {
        if (*i + 1 < argc) { (*i)++; return argv[*i]; }
        return "";
    }
    return NULL;
}

int main(int argc, char **argv)
{
    int n = gpu_count(), i, list = 0;
    const char *query = NULL, *format = NULL, *ids = NULL, *v;

    log_call(argc, argv);
    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (strcmp(a, "-L") == 0 || strcmp(a, "--list-gpus") == 0) { list = 1; continue; }
        if ((v = opt_value(argc, argv, &i, "--query-gpu", NULL)) != NULL) { query = v; continue; }
        if ((v = opt_value(argc, argv, &i, "--format", NULL)) != NULL) { format = v; continue; }
        if ((v = opt_value(argc, argv, &i, "--id", "-i")) != NULL) { ids = v; continue; }
        if (strcmp(a, "-h") == 0 || strcmp(a, "--help") == 0) {
            printf("NVIDIA System Management Interface -- v%s (ci_winmatrix stand-in)\n", DRIVER_VERSION);
            return 0;
        }
        if (strcmp(a, "--version") == 0) {
            printf("NVIDIA-SMI version  : %s\nDRIVER version      : %s\nCUDA Version        : %s\n",
                   DRIVER_VERSION, DRIVER_VERSION, env_or("FAKE_CUDA", "12.8"));
            return 0;
        }
        fprintf(stderr, "Invalid combination of input arguments. Please run 'nvidia-smi -h' for help.\n");
        return 2;
    }
    if (query) {
        if (n == 0) { printf("No devices were found\n"); return 6; }
        return run_query(query, format, ids, n);
    }
    if (list) {
        if (n == 0) { printf("No devices were found\n"); return 6; }
        print_list(n);
        return 0;
    }
    if (n == 0) { printf("No devices were found\n"); return 6; }
    print_banner(n);
    return 0;
}
