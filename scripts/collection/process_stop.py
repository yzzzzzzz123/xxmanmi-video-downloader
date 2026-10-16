"""Bounded, project-owned termination after the durable collection gate closes.

Shared servers are never OS termination targets. Their registered collection
terminals are stopped through the native API. No command line is put in receipts.
"""
import ctypes
import json
import os
import re
import subprocess
import shlex
import signal
import select
from ctypes import wintypes
from pathlib import Path


COLLECTION_COMMANDS = {
    'workflowctl.py', 'searchctl.py', 'coordctl.py', 'recover_search.py',
    'production_sync.py', 'production_live.py', 'catalogue_align.py',
    'mainctl.py', 'operationsctl.py', 'recoveryctl.py', 'playwrightctl.py', 'single_agentctl.py',
}
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)


def targets_for(config, main, coordination, recovery, project):
    """Fail closed before even enumerating execution targets."""
    if (config.get('force_process_stop_authorized') is not True
            or config.get('stop_authorized') is not True
            or not recovery.get('stopped')
            or not main.get('paused')
            or not coordination.get('collection_paused')):
        raise ValueError('Forced termination requires authorization and closed gates')
    if main.get('execution_mode') == 'single':
        owner = config.get('main_thread')
        binding = main.get('slots', {}).get('main', {})
        registered = coordination.get('threads', {}).get(owner, {})
        work = Path(binding.get('work', '')).resolve()
        if (not owner or not UUID.fullmatch(owner) or main.get('main_thread') != owner
                or set(main.get('slots', {})) != {'main'} or binding.get('owner') != owner
                or coordination.get('collection_active_owner') != owner
                or not registered.get('work') or Path(registered['work']).resolve() != work
                or not work.is_relative_to(project.resolve())):
            raise ValueError('Single agent stop bindings disagree')
        return {owner: work}
    controls = {config.get('main_thread'), config.get('host_parent_thread')}
    if (None in controls or len(controls) != 2
            or main.get('main_thread') != config['main_thread']
            or main.get('host_parent_thread') != config['host_parent_thread']):
        raise ValueError('Control identities disagree')
    result = {owner: None for owner in controls}
    if set(main.get('slots', {})) != {'01', '02', '03', '04'}:
        raise ValueError('Exactly the original four slots are required')
    for binding in main['slots'].values():
        owner = binding['owner']
        registered = coordination.get('threads', {}).get(owner, {})
        work = Path(binding['work']).resolve()
        if (owner in result or not UUID.fullmatch(owner)
                or not work.is_relative_to(project.resolve())
                or registered.get('role') not in {'collector', 'peer'}
                or not registered.get('work')
                or Path(registered['work']).resolve() != work):
            raise ValueError('Invalid collection owner binding')
        result[owner] = work
    if any(not UUID.fullmatch(owner) for owner in result):
        raise ValueError('Invalid registered identity')
    return result


def windows_argv(command):
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))


def dedicated_owner(argv, executable, owners, project):
    """Only an exact resume UUID or an absolute collection CLI + owner qualifies."""
    if not argv:
        return None
    name = Path(executable).name.lower()
    args = argv[1:]
    if name in {'chrome.exe','chromium.exe','chrome','chromium','headless_shell'} and isinstance(owners,dict):
        profile = next((a.split('=',1)[1] for a in args if a.startswith('--user-data-dir=')),None)
        if profile and any(a=='--headless' or a.startswith('--headless=') for a in args):
            for owner,work in owners.items():
                if work is not None and Path(profile).resolve()==Path(work).resolve()/'playwright-browser-profile':
                    return owner
        return None
    if name in {'codex.exe','codex'}:
        # An app-server may mention a UUID in its arguments; it is still shared.
        if not args or args[0] not in {'resume', 'exec'} or 'app-server' in args:
            return None
        for i, arg in enumerate(args[:-1]):
            if arg == 'resume' and args[i + 1] in owners:
                return args[i + 1]
        return None
    if name not in {'python.exe', 'pythonw.exe'} and not (os.name!='nt' and re.fullmatch(r'python(?:\d+(?:\.\d+)?)?',name)):
        return None
    entry = next((Path(a) for a in args if a.lower().endswith('.py')), None)
    if (entry is None or not entry.is_absolute()
            or entry.parent.resolve() != (project / 'scripts/collection').resolve()
            or entry.name not in COLLECTION_COMMANDS):
        return None
    for i, arg in enumerate(args[:-1]):
        if arg in {'--thread', '--owner'} and args[i + 1] in owners:
            return args[i + 1]
    return None


def process_snapshot():
    if os.name!='nt':
        rows=[]
        for folder in Path('/proc').iterdir():
            if not folder.name.isdigit():continue
            try:
                fields=(folder/'stat').read_text().split(') ',1)[1].split()
                arguments=[part.decode('utf-8','replace') for part in (folder/'cmdline').read_bytes().split(b'\0') if part]
                rows.append({'ProcessId':int(folder.name),'ParentProcessId':int(fields[1]),
                    'CreationDate':fields[19],'ExecutablePath':str((folder/'exe').resolve(strict=True)),
                    'CommandLine':shlex.join(arguments),'Arguments':arguments,
                    'cwd':str((folder/'cwd').resolve(strict=True))})
            except (OSError,ValueError,IndexError):continue
        return rows
    command = ('[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); '
               'Get-CimInstance Win32_Process | Select-Object ProcessId,'
               'ParentProcessId,CreationDate,ExecutablePath,CommandLine | '
               'ConvertTo-Json -Compress')
    result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
        capture_output=True, text=True, encoding='utf-8', timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    rows = json.loads(result.stdout or '[]')
    return rows if isinstance(rows, list) else [rows]


def protected_ancestors(rows, pid):
    parents = {row['ProcessId']: row['ParentProcessId'] for row in rows}
    protected = set()
    while pid and pid not in protected:
        protected.add(pid)
        pid = parents.get(pid)
    return protected


def terminate_exact(row):
    """Pin a process handle, verify image and creation time, then confirm exit."""
    if os.name!='nt':
        try:handle=os.pidfd_open(row['ProcessId'])
        except ProcessLookupError:return {'ok':True,'already_exited':True}
        try:
            folder=Path('/proc')/str(row['ProcessId'])
            try:
                start=(folder/'stat').read_text().split(') ',1)[1].split()[19]
                executable=str((folder/'exe').resolve(strict=True))
            except FileNotFoundError:return {'ok':True,'already_exited':True}
            if start!=row.get('CreationDate') or executable!=row.get('ExecutablePath'):
                raise ValueError('Process identity changed; refusing PID reuse')
            signal.pidfd_send_signal(handle,signal.SIGTERM)
            poll=select.poll();poll.register(handle,select.POLLIN)
            if not poll.poll(3000):signal.pidfd_send_signal(handle,signal.SIGKILL)
            exited=bool(poll.poll(3000))
            return {'ok':exited,'exit_verified':exited,'creation_start_ticks':start}
        finally:os.close(handle)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x1000 | 0x100000 | 0x0001, False, row['ProcessId'])
    if not handle:
        if ctypes.get_last_error() == 87:
            return {'ok': True, 'already_exited': True}
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return {'ok': True, 'already_exited': True}
        creation, exit_at, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, *[
                ctypes.byref(v) for v in (creation, exit_at, system, user)]):
            raise ctypes.WinError(ctypes.get_last_error())
        filetime = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        expected = re.fullmatch(r'/Date\((\d+)\)/', row.get('CreationDate') or '')
        if not expected or (filetime - 116444736000000000) // 10000 != int(expected[1]):
            raise ValueError('Process creation changed; refusing PID reuse')
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if os.path.normcase(buffer.value) != os.path.normcase(row['ExecutablePath']):
            raise ValueError('Process executable changed')
        if not kernel.TerminateProcess(handle, 1):
            if kernel.WaitForSingleObject(handle, 0) != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        exited = kernel.WaitForSingleObject(handle, 3000) == 0
        return {'ok': exited, 'exit_verified': exited, 'creation_filetime': filetime}
    finally:
        kernel.CloseHandle(handle)


def collection_terminal(row, work, project):
    """Collector terminals are exclusive; control-thread web/DB terminals stay."""
    cwd = Path(row.get('cwd') or '').resolve()
    if work is not None:
        return cwd in {work, project.resolve(), project.resolve().parent}
    if cwd != project.resolve():
        return False
    command = row.get('command') or ''
    return any(re.search(r'(?<![\w.-])' + re.escape(name) + r'(?![\w.-])', command)
               for name in COLLECTION_COMMANDS) and bool(
                   re.search(r'scripts[\\/]collection[\\/]', command))


def stop_owned_processes(config, main, coordination, recovery, project, rpc, caller_thread=None):
    owners = targets_for(config, main, coordination, recovery, project)
    # Enumerate once, before terminal termination, so the stop driver survives to
    # save its receipt. The snapshot's image/time are rechecked on pinned handles.
    try:
        rows = process_snapshot()
        protected = protected_ancestors(rows, os.getpid())
    except Exception as error:
        return {'ok': False, 'error_type': type(error).__name__,
                'shared_processes_terminated': False}
    terminal_receipts = {}
    for owner, work in owners.items():
        try:
            thread = rpc.call('thread/read', {'threadId': owner, 'includeTurns': False})['thread']
            if thread.get('id') != owner or Path(thread['cwd']).resolve() not in {
                    project.resolve(), project.resolve().parent}:
                raise ValueError('Native thread identity/cwd mismatch')
            if work is not None:
                source = thread.get('source') or {}
                spawned = source.get('subAgent', {}).get('thread_spawn', {}) if isinstance(source, dict) else {}
                parent = thread.get('parentThreadId') or spawned.get('parent_thread_id')
                if main.get('execution_mode')!='single' and parent != main['host_parent_thread']:
                    raise ValueError('Native collector parent mismatch')
            status = thread.get('status') or {}
            if isinstance(status, dict) and status.get('type') == 'notLoaded':
                terminal_receipts[owner] = {'ok': True, 'runtime_not_loaded': True}
                continue
            before = rpc.call('thread/backgroundTerminals/list', {'threadId': owner})
            if before.get('nextCursor'):
                raise ValueError('Incomplete terminal inventory; preserve checkpoint')
            killed = []
            kept = []
            drivers = []
            for terminal in before.get('data', []):
                pid = terminal['processId']  # Native terminal ID, never an OS PID.
                if not collection_terminal(terminal, work, project):
                    kept.append(pid)
                    continue
                command = terminal.get('command') or ''
                if (terminal.get('osPid') in protected or (owner == caller_thread
                        and re.search(r'(?:recoveryctl\.py\s+(?:stop|recover-stop-driver)\b|operationsctl\.py\s+pulse)', command))):
                    drivers.append(pid)
                    continue
                result = rpc.call('thread/backgroundTerminals/terminate',
                                  {'threadId': owner, 'processId': pid})
                killed.append({'process_id': pid, 'accepted': result.get('terminated') is True})
            after = rpc.call('thread/backgroundTerminals/list', {'threadId': owner})
            remaining = [t['processId'] for t in after.get('data', [])
                         if collection_terminal(t, work, project) and t['processId'] not in drivers]
            terminal_receipts[owner] = {'ok': not remaining and not after.get('nextCursor'),
                'terminated': killed, 'remaining': remaining, 'preserved': kept,
                'protected_stop_drivers': drivers}
        except Exception as error:
            terminal_receipts[owner] = {'ok': False, 'error_type': type(error).__name__}
    process_receipts = []
    try:
        for row in rows:
            if not row.get('ExecutablePath') or not row.get('CommandLine'):
                continue
            arguments=windows_argv(row['CommandLine']) if os.name=='nt' else list(row.get('Arguments',[]))
            if os.name!='nt':
                arguments=[str((Path(row['cwd'])/part).resolve()) if part.endswith('.py') and not Path(part).is_absolute() else part for part in arguments]
            owner = dedicated_owner(arguments,
                                    row['ExecutablePath'], owners, project)
            if not owner:
                continue
            receipt = {'pid': row['ProcessId'], 'owner': owner}
            if row['ProcessId'] in protected:
                receipt.update(ok=True, protected_stop_driver=True)
            else:
                try:
                    receipt.update(terminate_exact(row))
                except Exception as error:
                    receipt.update(ok=False, error_type=type(error).__name__)
            process_receipts.append(receipt)
    except Exception as error:
        process_receipts.append({'ok': False, 'error_type': type(error).__name__})
    return {'ok': all(r['ok'] for r in [*terminal_receipts.values(), *process_receipts]),
        'method': 'native_owned_terminals_and_verified_dedicated_'+('windows' if os.name=='nt' else 'linux')+'_processes',
        'thread_terminals': terminal_receipts, 'dedicated_processes': process_receipts,
        'shared_processes_terminated': False}
