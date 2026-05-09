#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define IIDA_DEVICE_TYPE 0x8000
#define IOCTL_READ_KERNEL_MEMORY  CTL_CODE(IIDA_DEVICE_TYPE, 0x800, METHOD_BUFFERED, FILE_READ_ACCESS)
#define IOCTL_GET_MODULE_LIST     CTL_CODE(IIDA_DEVICE_TYPE, 0x801, METHOD_BUFFERED, FILE_READ_ACCESS)
#define IOCTL_GET_MODULE_BASE     CTL_CODE(IIDA_DEVICE_TYPE, 0x802, METHOD_BUFFERED, FILE_READ_ACCESS)

#pragma pack(push, 1)
typedef struct _READ_KERNEL_REQUEST {
    uint64_t Address;
    uint32_t Size;
} READ_KERNEL_REQUEST;

typedef struct _MODULE_ENTRY {
    uint64_t Base;
    uint32_t Size;
    char Name[64];
    char Path[256];
} MODULE_ENTRY;

typedef struct _MODULE_LIST_HEADER {
    uint32_t Count;
} MODULE_LIST_HEADER;

typedef struct _MODULE_BASE_REQUEST {
    char Name[64];
} MODULE_BASE_REQUEST;

typedef struct _MODULE_BASE_RESPONSE {
    uint64_t Base;
    uint32_t Size;
} MODULE_BASE_RESPONSE;
#pragma pack(pop)

static void print_error(const char *msg)
{
    printf("{\"e\":\"%s\"}", msg);
}

static void print_win_error(const char *prefix)
{
    printf("{\"e\":\"%s: 0x%lx\"}", prefix, GetLastError());
}

static HANDLE open_device(void)
{
    HANDLE h = CreateFileW(L"\\\\.\\iida-mcp-ioctl", GENERIC_READ, 0, NULL, OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return NULL;
    return h;
}

static void json_string(const char *s)
{
    putchar('"');
    while (*s) {
        unsigned char c = (unsigned char)*s++;
        if (c == '\\' || c == '"') {
            putchar('\\');
            putchar(c);
        } else if (c >= 0x20) {
            putchar(c);
        }
    }
    putchar('"');
}

static int cmd_read(int argc, char **argv)
{
    HANDLE h;
    READ_KERNEL_REQUEST req;
    unsigned char *out;
    DWORD ret = 0;
    DWORD i;
    BOOL ok;

    if (argc < 4) {
        print_error("usage: read <hex_addr> <size>");
        return 2;
    }

    req.Address = _strtoui64(argv[2], NULL, 16);
    req.Size = strtoul(argv[3], NULL, 0);
    if (req.Size == 0 || req.Size > 65536) {
        print_error("bad size");
        return 2;
    }

    h = open_device();
    if (!h) {
        print_error("kernel driver iida-mcp-ioctl not loaded");
        return 3;
    }

    out = (unsigned char *)calloc(req.Size, 1);
    if (!out) {
        CloseHandle(h);
        print_error("out of memory");
        return 4;
    }

    ok = DeviceIoControl(h, IOCTL_READ_KERNEL_MEMORY, &req, sizeof(req), out, req.Size, &ret, NULL);
    if (!ok) {
        free(out);
        CloseHandle(h);
        print_win_error("read failed");
        return 5;
    }

    putchar('"');
    for (i = 0; i < ret; i++)
        printf("%02x", out[i]);
    putchar('"');

    free(out);
    CloseHandle(h);
    return 0;
}

static int cmd_modules(void)
{
    HANDLE h;
    unsigned char *out;
    DWORD outsz = sizeof(MODULE_LIST_HEADER) + sizeof(MODULE_ENTRY) * 2048;
    DWORD ret = 0;
    BOOL ok;
    MODULE_LIST_HEADER *hdr;
    MODULE_ENTRY *mods;
    DWORD i;

    h = open_device();
    if (!h) {
        print_error("kernel driver iida-mcp-ioctl not loaded");
        return 3;
    }

    out = (unsigned char *)calloc(outsz, 1);
    if (!out) {
        CloseHandle(h);
        print_error("out of memory");
        return 4;
    }

    ok = DeviceIoControl(h, IOCTL_GET_MODULE_LIST, NULL, 0, out, outsz, &ret, NULL);
    if (!ok) {
        free(out);
        CloseHandle(h);
        print_win_error("module list failed");
        return 5;
    }

    if (ret < sizeof(MODULE_LIST_HEADER)) {
        free(out);
        CloseHandle(h);
        print_error("truncated response");
        return 6;
    }

    hdr = (MODULE_LIST_HEADER *)out;
    mods = (MODULE_ENTRY *)(out + sizeof(MODULE_LIST_HEADER));
    putchar('[');
    for (i = 0; i < hdr->Count && sizeof(MODULE_LIST_HEADER) + (i + 1) * sizeof(MODULE_ENTRY) <= ret; i++) {
        if (i)
            putchar(',');
        printf("[\"%llx\",%lu,", (unsigned long long)mods[i].Base, (unsigned long)mods[i].Size);
        json_string(mods[i].Name);
        putchar(',');
        json_string(mods[i].Path);
        putchar(']');
    }
    putchar(']');

    free(out);
    CloseHandle(h);
    return 0;
}

static int cmd_base(int argc, char **argv)
{
    HANDLE h;
    MODULE_BASE_REQUEST req;
    MODULE_BASE_RESPONSE resp;
    DWORD ret = 0;
    BOOL ok;

    if (argc < 3) {
        print_error("usage: base <module>");
        return 2;
    }

    memset(&req, 0, sizeof(req));
    strncpy(req.Name, argv[2], sizeof(req.Name) - 1);

    h = open_device();
    if (!h) {
        print_error("kernel driver iida-mcp-ioctl not loaded");
        return 3;
    }

    memset(&resp, 0, sizeof(resp));
    ok = DeviceIoControl(h, IOCTL_GET_MODULE_BASE, &req, sizeof(req), &resp, sizeof(resp), &ret, NULL);
    if (!ok) {
        CloseHandle(h);
        print_win_error("module base failed");
        return 5;
    }

    printf("[\"%llx\",%lu]", (unsigned long long)resp.Base, (unsigned long)resp.Size);
    CloseHandle(h);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        print_error("usage: read/modules/base");
        return 2;
    }
    if (strcmp(argv[1], "read") == 0)
        return cmd_read(argc, argv);
    if (strcmp(argv[1], "modules") == 0)
        return cmd_modules();
    if (strcmp(argv[1], "base") == 0)
        return cmd_base(argc, argv);
    print_error("unknown command");
    return 2;
}
