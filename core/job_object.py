

import sys
import subprocess
import logging

logger = logging.getLogger(__name__)

_job_handle = None


def _init_job_object():
    """Создаёт Job Object с JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        kernel32.CreateJobObjectW.restype  = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

        kernel32.SetInformationJobObject.restype  = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]

        kernel32.OpenProcess.restype  = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

        kernel32.AssignProcessToJobObject.restype  = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        kernel32.CloseHandle.restype  = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning("[JobObject] CreateJobObjectW failed")
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit",     ctypes.c_int64),
                ("LimitFlags",              wintypes.DWORD),
                ("MinimumWorkingSetSize",   ctypes.c_size_t),
                ("MaximumWorkingSetSize",   ctypes.c_size_t),
                ("ActiveProcessLimit",      wintypes.DWORD),
                ("Affinity",                ctypes.c_size_t),
                ("PriorityClass",           wintypes.DWORD),
                ("SchedulingClass",         wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount",  ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount",   ctypes.c_uint64),
                ("WriteTransferCount",  ctypes.c_uint64),
                ("OtherTransferCount",  ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo",                IO_COUNTERS),
                ("ProcessMemoryLimit",    ctypes.c_size_t),
                ("JobMemoryLimit",        ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed",     ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        success = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not success:
            err = ctypes.get_last_error()
            logger.warning("[JobObject] SetInformationJobObject failed (GetLastError=%d)", err)
            kernel32.CloseHandle(job)
            return None

        logger.info("[JobObject] создан: KILL_ON_JOB_CLOSE (sizeof=%d)", ctypes.sizeof(info))
        return job

    except Exception as e:
        logger.warning("[JobObject] init error: %s", e)
        return None


def assign_to_job(proc: subprocess.Popen) -> bool:
    global _job_handle
    if _job_handle is None:
        return False

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32

        PROCESS_ALL_ACCESS = 0x1F0FFF
        handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
        if not handle:
            return False

        result = kernel32.AssignProcessToJobObject(_job_handle, handle)
        kernel32.CloseHandle(handle)

        if result:
            logger.debug("[JobObject] PID %d привязан к Job", proc.pid)
            return True
        else:
            logger.warning("[JobObject] AssignProcessToJobObject failed для PID %d", proc.pid)
            return False

    except Exception as e:
        logger.warning("[JobObject] assign error PID %d: %s", proc.pid, e)
        return False

_job_handle = _init_job_object()
