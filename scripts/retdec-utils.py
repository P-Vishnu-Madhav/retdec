#!/usr/bin/env python3

"""Compilation and decompilation utility functions.
"""
import os
import re
import shutil
import signal
import string
import subprocess
import sys
import time

import importlib
config = importlib.import_module('retdec-config')


TIMEOUT_RC = 137


"""Taken from https://github.com/avast-tl/retdec-regression-tests-framework/blob/master/regression_tests/cmd_runner.py
"""


class CmdRunner:
    """A runner of external commands."""

    def run_cmd(self, cmd, input='', timeout=None, buffer_output=False):
        """Runs the given command (synchronously).

        :param list cmd: Command to be run as a list of arguments (strings).
        :param str input: Input to be used when running the command.
        :param int timeout: Number of seconds after which the command should be
                            terminated.
        :param bool buffer_output: Should the output be buffered and returned
                            in `output` instead of printing it to stdout?
                            If `False`, `None` is returned in `output`.

        :returns: A triple (`output`, `return_code`, `timeouted`).

        The meaning of the items in the return value are:

        * `output` if `buffer_output` is `True` contains the combined output from
          the standard outputs and standard error,
        * `return_code` is the return code of the command,
        * `timeouted` is either `True` or `False`, depending on whether the
          command has timeouted.

        To disable the timeout, pass ``None`` as `timeout` (the default).

        If the timeout expires before the command finishes, the value of `output`
        is the command's output generated up to the timeout.
        """
        _, output, return_code, timeouted = self._run_cmd(cmd, input, timeout, buffer_output, track_memory=False)

        return output, return_code, timeouted

    def run_measured_cmd(self, cmd, input='', timeout=None):
        """Runs the given command (synchronously) and measure its time and memory.
        :param list cmd: Command to be run as a list of arguments (strings).

        :returns: A quadruple (`memory`, `elapsed_time`, `output`, `return_code`).
        """
        start = time.time()
        memory, output, rc, _ = CmdRunner()._run_cmd(cmd, input, timeout, buffer_output=True, track_memory=True)
        elapsed = int(time.time() - start)
        if elapsed == 0:
            elapsed = 1
        return memory, elapsed, output, rc

    def _run_cmd(self, cmd, input='', timeout=None, buffer_output=False, track_memory=False):
        """:returns: A quadruple (`memory`, `output`, `return_code`, `timeouted`)."""
        memory = 0
        try:
            track_memory = track_memory and Utils.tool_exists(config.LOG_TIME[0])
            if track_memory:
                cmd = config.LOG_TIME + cmd

            p = self.start(cmd, buffer_output)

            def signal_handler(sig, frame):
                p.kill()
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

            output, _ = p.communicate(input, timeout)

            if output:
                output = output.rstrip()
                output = self._strip_shell_colors(output)
                if track_memory:
                    memory = self._get_memory_from_measured_output(output)
                    output = self._get_clean_output_from_measured_output(output)
            return memory, output, p.returncode, False
        except subprocess.TimeoutExpired:
            # Kill the process, along with all its child processes.
            p.kill()
            # Finish the communication to obtain the output.
            output, _ = p.communicate()
            if output:
                output = output.rstrip()
                output = self._strip_shell_colors(output)
            return memory, output, TIMEOUT_RC, True

    def start(self, cmd, buffer_output=False, stdout=subprocess.STDOUT):
        """Starts the given command and returns a handler to it.

        :param list cmd: Command to be run as a list of arguments (strings).
        :param bool buffer_output: See above.
        :param int stdout: If buffer_output is True, errors will be redirectected
                            to the stdout param.

        :returns: A handler to the started command (``subprocess.Popen``).

        If the output is irrelevant for you, you should set `buffer_output` to
        ``True`` and ignore the `output`.
        """
        # The implementation is platform-specific because we want to be able to
        # kill the children alongside with the process.
        kwargs = dict(
            args=cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if buffer_output else None,
            stderr=stdout if buffer_output else None,
            universal_newlines=True
        )
        if Utils.is_windows():
            return _WindowsProcess(**kwargs)
        else:
            return _LinuxProcess(**kwargs)

    def _strip_shell_colors(self, text):
        """Strips shell colors from the given text."""
        return re.sub(r'\x1b[^m]*m', '', text)

    def _get_memory_from_measured_output(self, output):
        """Get memory in MB from output string generated by `config.LOG_TIME`.
        `/usr/bin/time` format is expected."""
        memory = 0
        s = re.search(r'Maximum resident set size \(kbytes\): (\d+)', output)
        if s:
            g = s.group(1)
            if g:
                memory_kb = int(g)
                memory = int(memory_kb / 1024) # to MB
                if memory == 0:
                    memory = 1
        return memory

    def _get_clean_output_from_measured_output(self, output):
        """Get the original output of the executed command from the measured
        output containing additional information.

        `/usr/bin/time` format is expected.


        The output from `/usr/bin/time -v` looks either like this (success):

            [..] (output from the tool)
                Command being timed: "tool"
                [..] (other data)

        or like this (when there was an error):

        [..] (output from the tool)
            Command exited with non-zero status X
            [..] (other data)
        """
        out = output
        out = out.split('Command being timed:')[0]
        out = out.split('Command exited with non-zero status')[0]
        out = out.rstrip()
        return out

class _LinuxProcess(subprocess.Popen):
    """An internal wrapper around ``subprocess.Popen`` for Linux."""

    def __init__(self, **kwargs):
        # To ensure that all the process' children terminate when the process
        # is killed, we use a process group so as to enable sending a signal to
        # all the processes in the group. For that, we attach a session ID to
        # the parent process of the spawned child processes. This will make it
        # the group leader of the processes. When a signal is sent to the
        # process group leader, it's transmitted to all of the child processes
        # of this group.
        #
        # os.setsid is passed in the argument preexec_fn so it's run after
        # fork() and before exec().
        #
        # This solution is based on http://stackoverflow.com/a/4791612.
        kwargs['preexec_fn'] = os.setsid
        super().__init__(**kwargs)

    def kill(self):
        """Kills the process, including its children."""
        os.killpg(self.pid, signal.SIGTERM)


class _WindowsProcess(subprocess.Popen):
    """An internal wrapper around ``subprocess.Popen`` for Windows."""

    def __init__(self, **kwargs):
        # Python scripts need to be run with 'python' on Windows. Simply running the
        # script by its path doesn't work. That is, for example, instead of
        #
        #     /path/to/retdec-decompiler.py
        #
        # we need to run
        #
        #     python /path/to/retdec-decompiler.py
        #
        if 'args' in kwargs and kwargs['args'] and kwargs['args'][0].endswith('.py'):
            kwargs['args'].insert(0, sys.executable)
        super().__init__(**kwargs)

    def kill(self):
        """Kills the process, including its children."""
        # Since os.setsid() and os.killpg() are not available on Windows, we
        # have to do this differently. More specifically, we do this by calling
        # taskkill, which also kills the process' children.
        #
        # This solution is based on
        # http://mackeblog.blogspot.cz/2012/05/killing-subprocesses-on-windows-in.html
        cmd = ['taskkill', '/F', '/T', '/PID', str(self.pid)]
        subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Utils:

    @staticmethod
    def tool_exists(tool_name):
        return shutil.which(tool_name) is not None

    @staticmethod
    def remove_file_forced(file):
        if os.path.exists(file):
            os.remove(file)

    @staticmethod
    def is_windows():
        return sys.platform in ('win32', 'msys') or os.name == 'nt'

    @staticmethod
    def print_error(error):
        """Print error message to stderr.
        """
        print('Error: %s' % error, file=sys.stderr)

    @staticmethod
    def print_error_and_die(error, ret_code=1):
        """Print error message to stderr, and exit with the given return code.
        """
        Utils.print_error(error)
        sys.exit(ret_code)

    @staticmethod
    def print_warning(warning):
        """Print warning message to stderr.
        """
        print('Warning: %s' % warning, file=sys.stderr)

    @staticmethod
    def has_archive_signature(path):
        """Check if file has any ar signature.
        1 argument is needed - file path
        Returns - True if file has ar signature
                  False no signature
        """
        ret = subprocess.call([config.AR, path, '--arch-magic'])
        return ret == 0

    @staticmethod
    def has_thin_archive_signature(path):
        """Check if file has thin ar signature.
        1 argument is needed - file path
        Returns - True if file has thin ar signature
                  False no signature
        """
        ret = subprocess.call([config.AR, path, '--thin-magic'])
        return ret == 0

    @staticmethod
    def is_valid_archive(path):
        """Check if file is an archive we can work with.
        1 argument is needed - file path
        Returns - True if file is valid archive
                  False if file is invalid archive
        """
        # We use our own messages so throw original output away.
        ret = subprocess.call([config.AR, path, '--valid'], stderr=subprocess.STDOUT,
                              stdout=None)
        return ret == 0

    @staticmethod
    def archive_object_count(path):
        """Counts object files in archive.
        1 argument is needed - file path
        Returns - 1 if error occurred
        """
        output, rc, _ = CmdRunner().run_cmd([config.AR, path, '--object-count'], buffer_output=True)
        return int(output) if rc == 0 else 1

    @staticmethod
    def archive_list_content(path):
        """Print content of archive.
        1 argument is needed - file path
        """
        CmdRunner().run_cmd([config.AR, path, '--list', '--no-numbers'])

    @staticmethod
    def archive_list_numbered_content(path):
        """Print numbered content of archive.
        1 argument is needed - file path
        """
        print('Index\tName')
        CmdRunner().run_cmd([config.AR, path, '--list'])

    @staticmethod
    def archive_list_numbered_content_json(path):
        """Print numbered content of archive in JSON format.
        1 argument is needed - file path
        """
        CmdRunner().run_cmd([config.AR, path, '--list', '--json'])

    @staticmethod
    def archive_get_by_name(path, name, output):
        """Get a single file from archive by name.
        3 arguments are needed - path to the archive
                               - name of the file
                               - output path
        Returns - False if everything ok
                  True if error
        """
        ret = subprocess.call([config.AR, path, '--name', name, '--output', output],
                              stderr=subprocess.STDOUT, stdout=None)
        return ret != 0

    @staticmethod
    def archive_get_by_index(archive, index, output):
        """Get a single file from archive by index.
        3 arguments are needed - path to the archive
                               - index of the file
                               - output path
        Returns - False if everything ok
                  True if error
        """
        ret = subprocess.call([config.AR, archive, '--index', index, '--output', output],
                              stderr=subprocess.STDOUT, stdout=None)
        return ret != 0

    @staticmethod
    def is_macho_archive(path):
        """Check if file is Mach-O universal binary with archives.
        1 argument is needed - file path
        Returns - True if file is archive
                  False if file is not archive
        """
        ret = subprocess.call([config.EXTRACT, '--check-archive', path],
                              stderr=subprocess.STDOUT, stdout=subprocess.DEVNULL)
        return ret == 0

    @staticmethod
    def is_decimal_number(num):
        return re.search('^[0-9]+$', str(num))

    @staticmethod
    def is_hexadecimal_number(num):
        return re.search('^0x[0-9a-fA-F]+$', str(num))

    @staticmethod
    def is_number(num):
        return Utils.is_decimal_number(num) or Utils.is_hexadecimal_number(num)

    @staticmethod
    def is_decimal_range(range):
        return re.search('^[0-9]+-[0-9]+$', str(range))

    @staticmethod
    def is_hexadecimal_range(range):
        return re.search('^0x[0-9a-fA-F]+-0x[0-9a-fA-F]+$', str(range))

    @staticmethod
    def is_range(range):
        return Utils.is_decimal_range(range) or Utils.is_hexadecimal_range(range)


class Unbuffered(object):
    """Used to force unbuddered streams, otherwise redirecting to log files
    might mix the outputs from Python scripts and executed tools.
    https://stackoverflow.com/a/107717"""
    def __init__(self, stream):
        self.stream = stream
    def write(self, data):
        self.stream.write(data)
        self.stream.flush()
    def writelines(self, datas):
        self.stream.writelines(datas)
        self.stream.flush()
    def __getattr__(self, attr):
        return getattr(self.stream, attr)
