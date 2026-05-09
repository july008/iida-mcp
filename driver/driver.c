#include "driver.h"

typedef struct _RTL_PROCESS_MODULE_INFORMATION {
    HANDLE Section;
    PVOID  MappedBase;
    PVOID  ImageBase;
    ULONG  ImageSize;
    ULONG  Flags;
    USHORT LoadOrderIndex;
    USHORT InitOrderIndex;
    USHORT LoadCount;
    USHORT OffsetToFileName;
    UCHAR  FullPathName[256];
} RTL_PROCESS_MODULE_INFORMATION, *PRTL_PROCESS_MODULE_INFORMATION;

typedef struct _RTL_PROCESS_MODULES {
    ULONG NumberOfModules;
    RTL_PROCESS_MODULE_INFORMATION Modules[1];
} RTL_PROCESS_MODULES, *PRTL_PROCESS_MODULES;

NTSYSAPI NTSTATUS NTAPI ZwQuerySystemInformation(
    ULONG SystemInformationClass,
    PVOID SystemInformation,
    ULONG SystemInformationLength,
    PULONG ReturnLength
);

#define SystemModuleInformation 11

static UNICODE_STRING g_DeviceName = RTL_CONSTANT_STRING(L"\\Device\\iida-mcp-ioctl");
static UNICODE_STRING g_SymLink   = RTL_CONSTANT_STRING(L"\\DosDevices\\iida-mcp-ioctl");
static PDEVICE_OBJECT g_DeviceObject = NULL;

static NTSTATUS HandleReadKernelMemory(PIRP Irp, PIO_STACK_LOCATION IoSp)
{
    READ_KERNEL_REQUEST req;
    PVOID outBuf;
    ULONG outLen;
    PVOID srcAddr;
    MM_COPY_ADDRESS src;
    SIZE_T bytesRead = 0;
    NTSTATUS status;

    if (IoSp->Parameters.DeviceIoControl.InputBufferLength < sizeof(READ_KERNEL_REQUEST))
        return STATUS_BUFFER_TOO_SMALL;

    RtlCopyMemory(&req, Irp->AssociatedIrp.SystemBuffer, sizeof(req));
    outBuf = Irp->AssociatedIrp.SystemBuffer;
    outLen = IoSp->Parameters.DeviceIoControl.OutputBufferLength;

    if (req.Size == 0 || req.Size > outLen)
        return STATUS_BUFFER_TOO_SMALL;

    if (req.Address < 0xFFFF000000000000ULL)
        return STATUS_INVALID_PARAMETER;

    srcAddr = (PVOID)req.Address;
    src.VirtualAddress = srcAddr;
    status = MmCopyMemory(outBuf, src, req.Size, MM_COPY_MEMORY_VIRTUAL, &bytesRead);

    Irp->IoStatus.Information = bytesRead;
    return status;
}

static NTSTATUS HandleGetModuleList(PIRP Irp, PIO_STACK_LOCATION IoSp)
{
    NTSTATUS status;
    ULONG retLen = 0;
    PRTL_PROCESS_MODULES mods = NULL;
    PVOID outBuf;
    ULONG outLen;
    ULONG i, count, needed;
    PMODULE_LIST_HEADER hdr;
    PMODULE_ENTRY entries;

    status = ZwQuerySystemInformation(SystemModuleInformation, NULL, 0, &retLen);
    if (retLen == 0)
        return STATUS_UNSUCCESSFUL;

    mods = (PRTL_PROCESS_MODULES)ExAllocatePool2(POOL_FLAG_NON_PAGED, retLen, 'iiDM');
    if (!mods)
        return STATUS_INSUFFICIENT_RESOURCES;

    status = ZwQuerySystemInformation(SystemModuleInformation, mods, retLen, &retLen);
    if (!NT_SUCCESS(status)) {
        ExFreePoolWithTag(mods, 'iiDM');
        return status;
    }

    outBuf = Irp->AssociatedIrp.SystemBuffer;
    outLen = IoSp->Parameters.DeviceIoControl.OutputBufferLength;
    count = mods->NumberOfModules;
    needed = sizeof(MODULE_LIST_HEADER) + count * sizeof(MODULE_ENTRY);

    if (outLen < needed) {
        if (outLen >= sizeof(MODULE_LIST_HEADER)) {
            hdr = (PMODULE_LIST_HEADER)outBuf;
            hdr->Count = 0;
            Irp->IoStatus.Information = sizeof(MODULE_LIST_HEADER);
            *(ULONG*)((PUCHAR)outBuf + sizeof(MODULE_LIST_HEADER) - sizeof(ULONG)) = needed;
        }
        ExFreePoolWithTag(mods, 'iiDM');
        return STATUS_BUFFER_OVERFLOW;
    }

    hdr = (PMODULE_LIST_HEADER)outBuf;
    hdr->Count = count;
    entries = (PMODULE_ENTRY)((PUCHAR)outBuf + sizeof(MODULE_LIST_HEADER));

    for (i = 0; i < count; i++) {
        PRTL_PROCESS_MODULE_INFORMATION m = &mods->Modules[i];
        MODULE_ENTRY *e = &entries[i];
        RtlZeroMemory(e, sizeof(MODULE_ENTRY));
        e->Base = (ULONG64)m->ImageBase;
        e->Size = m->ImageSize;
        RtlCopyMemory(e->Path, m->FullPathName, 255);
        e->Path[255] = '\0';
        RtlCopyMemory(e->Name, &m->FullPathName[m->OffsetToFileName], 63);
        e->Name[63] = '\0';
    }

    Irp->IoStatus.Information = needed;
    ExFreePoolWithTag(mods, 'iiDM');
    return STATUS_SUCCESS;
}

static NTSTATUS HandleGetModuleBase(PIRP Irp, PIO_STACK_LOCATION IoSp)
{
    NTSTATUS status;
    ULONG retLen = 0;
    PRTL_PROCESS_MODULES mods = NULL;
    PMODULE_BASE_REQUEST req;
    PMODULE_BASE_RESPONSE resp;
    ULONG i;
    SIZE_T nameLen;

    if (IoSp->Parameters.DeviceIoControl.InputBufferLength < sizeof(MODULE_BASE_REQUEST))
        return STATUS_BUFFER_TOO_SMALL;
    if (IoSp->Parameters.DeviceIoControl.OutputBufferLength < sizeof(MODULE_BASE_RESPONSE))
        return STATUS_BUFFER_TOO_SMALL;

    req = (PMODULE_BASE_REQUEST)Irp->AssociatedIrp.SystemBuffer;
    req->Name[63] = '\0';

    status = ZwQuerySystemInformation(SystemModuleInformation, NULL, 0, &retLen);
    if (retLen == 0)
        return STATUS_UNSUCCESSFUL;

    mods = (PRTL_PROCESS_MODULES)ExAllocatePool2(POOL_FLAG_NON_PAGED, retLen, 'iiDM');
    if (!mods)
        return STATUS_INSUFFICIENT_RESOURCES;

    status = ZwQuerySystemInformation(SystemModuleInformation, mods, retLen, &retLen);
    if (!NT_SUCCESS(status)) {
        ExFreePoolWithTag(mods, 'iiDM');
        return status;
    }

    nameLen = strlen(req->Name);
    for (i = 0; i < mods->NumberOfModules; i++) {
        PRTL_PROCESS_MODULE_INFORMATION m = &mods->Modules[i];
        const char *modName = (const char *)&m->FullPathName[m->OffsetToFileName];
        if (_strnicmp(modName, req->Name, nameLen) == 0 && (modName[nameLen] == '\0' || modName[nameLen] == '.')) {
            resp = (PMODULE_BASE_RESPONSE)Irp->AssociatedIrp.SystemBuffer;
            resp->Base = (ULONG64)m->ImageBase;
            resp->Size = m->ImageSize;
            Irp->IoStatus.Information = sizeof(MODULE_BASE_RESPONSE);
            ExFreePoolWithTag(mods, 'iiDM');
            return STATUS_SUCCESS;
        }
    }

    ExFreePoolWithTag(mods, 'iiDM');
    return STATUS_NOT_FOUND;
}

static NTSTATUS DispatchDeviceControl(PDEVICE_OBJECT DeviceObject, PIRP Irp)
{
    PIO_STACK_LOCATION ioSp = IoGetCurrentIrpStackLocation(Irp);
    NTSTATUS status = STATUS_INVALID_DEVICE_REQUEST;

    UNREFERENCED_PARAMETER(DeviceObject);

    Irp->IoStatus.Information = 0;

    switch (ioSp->Parameters.DeviceIoControl.IoControlCode) {
    case IOCTL_READ_KERNEL_MEMORY:
        status = HandleReadKernelMemory(Irp, ioSp);
        break;
    case IOCTL_GET_MODULE_LIST:
        status = HandleGetModuleList(Irp, ioSp);
        break;
    case IOCTL_GET_MODULE_BASE:
        status = HandleGetModuleBase(Irp, ioSp);
        break;
    }

    Irp->IoStatus.Status = status;
    IoCompleteRequest(Irp, IO_NO_INCREMENT);
    return status;
}

static NTSTATUS DispatchCreateClose(PDEVICE_OBJECT DeviceObject, PIRP Irp)
{
    UNREFERENCED_PARAMETER(DeviceObject);
    Irp->IoStatus.Status = STATUS_SUCCESS;
    Irp->IoStatus.Information = 0;
    IoCompleteRequest(Irp, IO_NO_INCREMENT);
    return STATUS_SUCCESS;
}

VOID DriverUnload(PDRIVER_OBJECT DriverObject)
{
    UNREFERENCED_PARAMETER(DriverObject);
    IoDeleteSymbolicLink(&g_SymLink);
    if (g_DeviceObject)
        IoDeleteDevice(g_DeviceObject);
}

NTSTATUS DriverEntry(PDRIVER_OBJECT DriverObject, PUNICODE_STRING RegistryPath)
{
    NTSTATUS status;

    UNREFERENCED_PARAMETER(RegistryPath);

    status = IoCreateDevice(DriverObject, 0, &g_DeviceName, IIDA_DEVICE_TYPE, 0, FALSE, &g_DeviceObject);
    if (!NT_SUCCESS(status))
        return status;

    status = IoCreateSymbolicLink(&g_SymLink, &g_DeviceName);
    if (!NT_SUCCESS(status)) {
        IoDeleteDevice(g_DeviceObject);
        g_DeviceObject = NULL;
        return status;
    }

    DriverObject->MajorFunction[IRP_MJ_CREATE] = DispatchCreateClose;
    DriverObject->MajorFunction[IRP_MJ_CLOSE]  = DispatchCreateClose;
    DriverObject->MajorFunction[IRP_MJ_DEVICE_CONTROL] = DispatchDeviceControl;
    DriverObject->DriverUnload = DriverUnload;

    return STATUS_SUCCESS;
}
