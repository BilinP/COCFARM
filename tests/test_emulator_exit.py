"""BlueStacks inventory and opt-in positive exit checks; no native input."""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from farmbot import bluestacks, ports
from farmbot.config import DeviceConfig
from farmbot.device import DeviceError


class EmulatorExitTests(unittest.TestCase):
    def setUp(self):
        self.bs = bluestacks.Install(Path('BlueStacks'), Path('data'), True,
                                     [bluestacks.Instance('Pie64', 'Main', 5555),
                                      bluestacks.Instance('Pie64_1', 'Second', 5565)])
        windows = patch.object(bluestacks.sys, 'platform', 'win32')
        windows.start()
        self.addCleanup(windows.stop)


    def test_bluestacks_successful_empty_inventory_is_stopped(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(bluestacks.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertFalse(bool(bluestacks.emulator_identity(DeviceConfig())))


    def test_bluestacks_exit_query_failure_is_unknown(self):
        for result in [subprocess.CompletedProcess([], 1, '', 'denied'),
                       subprocess.CompletedProcess([], 0, '', 'query failed')]:
            with self.subTest(result=result), patch.object(bluestacks, 'find_install', return_value=self.bs), \
                    patch.object(bluestacks.subprocess, 'run', return_value=result):
                with self.assertRaises(DeviceError):
                    bluestacks.emulator_identity(DeviceConfig())


    def test_bluestacks_query_exception_is_unknown(self):
        with patch.object(bluestacks.subprocess, 'run', side_effect=OSError('denied')):
            with self.assertRaises(DeviceError):
                bluestacks.players(strict=True)


    def test_bluestacks_strict_rejects_unreadable_or_ambiguous_rows(self):
        for listing in ['12\t', 'no native inventory', '12\tHD-Player.exe --instance',
                        '12\tother.exe', '0\tHD-Player.exe',
                        '12\tHD-Player.exe\n12\tHD-Player.exe']:
            with self.subTest(listing=listing), self.assertRaises(DeviceError):
                bluestacks.players(listing, strict=True)


    def test_bluestacks_legacy_parser_remains_permissive(self):
        self.assertEqual(bluestacks.players('nonsense\n12\t'), [(12, '')])
        with patch.object(bluestacks.subprocess, 'run', side_effect=OSError('denied')):
            self.assertEqual(bluestacks.players(), [])


    def test_bluestacks_hidden_command_line_uses_unique_exact_window_title(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(ports, 'process_path', return_value=self.bs.player_exe), \
                patch.object(bluestacks.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, '123\t\tSecond', '')):
            self.assertEqual(bluestacks.emulator_identity(
                DeviceConfig(bluestacks_instance='Pie64_1')), (123,))


    def test_bluestacks_hidden_command_line_requires_installed_player_image(self):
        for image in [None, Path('other/HD-Player.exe')]:
            with self.subTest(image=image), patch.object(bluestacks, 'find_install', return_value=self.bs), \
                    patch.object(ports, 'process_path', return_value=image), self.assertRaises(DeviceError):
                bluestacks.players('123\t\tSecond', strict=True)


    def test_bluestacks_hidden_command_line_rejects_missing_unknown_or_duplicate_titles(self):
        for title in ['', 'Second - loading', 'Second ', 'Unknown']:
            with self.subTest(title=title), patch.object(bluestacks, 'find_install', return_value=self.bs), \
                    self.assertRaises(DeviceError):
                bluestacks.players('123\t\t' + title, strict=True)
        self.bs.instances.append(bluestacks.Instance('Pie64_2', 'Second', 5575))
        with patch.object(bluestacks, 'find_install', return_value=self.bs), self.assertRaises(DeviceError):
            bluestacks.players('123\t\tSecond', strict=True)


    def test_bluestacks_native_inventory_waits_for_another_starting_window(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(ports, 'process_path', return_value=self.bs.player_exe), \
                patch.object(bluestacks.time, 'sleep') as sleep, \
                patch.object(bluestacks.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, '123\tHD-Player.exe --instance Pie64\n124\t\t', ''),
                    subprocess.CompletedProcess([], 0, '123\tHD-Player.exe --instance Pie64\n124\t\tSecond', '')]):
            self.assertEqual(bluestacks.emulator_identity(DeviceConfig()), (123,))
            sleep.assert_called_once_with(1)


    def test_bluestacks_native_inventory_waits_for_an_unidentified_exiting_window(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(bluestacks.time, 'sleep') as sleep, \
                patch.object(bluestacks.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, '123\t\t', ''),
                    subprocess.CompletedProcess([], 0, '', '')]):
            self.assertEqual(bluestacks.emulator_identity(DeviceConfig()), ())
            sleep.assert_called_once_with(1)


    def test_bluestacks_missing_window_metadata_is_bounded_not_absent(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(bluestacks.time, 'sleep') as sleep, \
                patch.object(bluestacks.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, '123\t\t', '')) as query:
            with self.assertRaises(DeviceError):
                bluestacks.emulator_identity(DeviceConfig())
            self.assertEqual(query.call_count, 11)
            self.assertEqual(sleep.call_count, 10)


    def test_bluestacks_named_unknown_window_is_not_retried(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(bluestacks.time, 'sleep') as sleep, \
                patch.object(bluestacks.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, '123\t\tUnknown window', '')) as query:
            with self.assertRaises(DeviceError):
                bluestacks.emulator_identity(DeviceConfig())
            query.assert_called_once()
            sleep.assert_not_called()


    def test_bluestacks_instance_stop_targets_verified_pid_and_waits_for_absence(self):
        with patch.object(bluestacks, 'emulator_identity', return_value=(123,)) as identity, \
                patch.object(bluestacks, '_pids_running', side_effect=[True, False], create=True) as running, \
                patch.object(bluestacks.subprocess, 'run') as kill:
            bluestacks.stop_player(sleep=lambda _: None, verify_exit=True, instance='Second', main='Pie64')
            self.assertEqual(kill.call_args.args[0], ['taskkill', '/PID', '123', '/F'])
            identity.assert_called_once()
            self.assertEqual(running.call_args.args, ((123,),))


    def test_bluestacks_instance_exit_poll_does_not_use_title_based_callback(self):
        with patch.object(bluestacks, 'emulator_identity', return_value=(123,)), \
                patch.object(bluestacks, '_pids_running', return_value=False), \
                patch.object(bluestacks.subprocess, 'run'):
            def stale_title_check():
                raise AssertionError('title disappeared during shutdown')
            bluestacks.stop_player(sleep=lambda _: None, verify_exit=True, running=stale_title_check, instance='Second')


    def test_bluestacks_unknown_stop_never_kills_and_resident_process_does_not_pass(self):
        with patch.object(bluestacks, 'emulator_identity', side_effect=DeviceError('unknown')), \
                patch.object(bluestacks.subprocess, 'run') as kill, self.assertRaises(DeviceError):
            bluestacks.stop_player(sleep=lambda _: None, verify_exit=True, instance='Second')
        kill.assert_not_called()
        with patch.object(bluestacks, 'emulator_identity', return_value=(123,)), \
                patch.object(bluestacks, '_pids_running', return_value=True, create=True), \
                patch.object(bluestacks.subprocess, 'run'), self.assertRaises(DeviceError):
            bluestacks.stop_player(sleep=lambda _: None, verify_exit=True, instance='Second')


    def test_bluestacks_pid_poll_does_not_need_a_window_title(self):
        for output, expected in [('123\n', True), ('', False)]:
            with self.subTest(output=output), patch.object(bluestacks.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, output, '')) as query:
                self.assertEqual(bluestacks._pids_running((123,)), expected)
                self.assertIn('ProcessId=123', query.call_args.args[0][-1])
                self.assertNotIn('MainWindowTitle', query.call_args.args[0][-1])


    def test_bluestacks_pid_poll_cannot_treat_query_failure_as_exit(self):
        for code, output, error in [(1, '', ''), (0, '', 'denied'), (0, 'unknown', '')]:
            with self.subTest(code=code, output=output, error=error), \
                    patch.object(bluestacks.subprocess, 'run',
                        return_value=subprocess.CompletedProcess([], code, output, error)), \
                    self.assertRaises(DeviceError):
                bluestacks._pids_running((123,))


    def test_bluestacks_strict_instance_argument_must_be_complete_and_unique(self):
        for args in ['--instance="Pie64 bad"', '--instance="Pie64', '--instance=Pie64"',
                     '--instance Pie64 --instance Pie64_1', '--instance=Pie64 --instance=Pie64']:
            with self.subTest(args=args), self.assertRaises(DeviceError):
                bluestacks.players('123\tHD-Player.exe ' + args, strict=True)
        for args in ['--instance Pie64', '--instance=Pie64', '--instance "Pie64"', '--instance="Pie64"']:
            with self.subTest(args=args):
                self.assertEqual(bluestacks.players('123\tHD-Player.exe '+args, strict=True), [(123, 'Pie64')])


    def test_bluestacks_identity_rejects_unknown_instances_and_duplicate_aliases(self):
        for listing in ['123\tHD-Player.exe --instance missing',
                        '123\tHD-Player.exe\n124\tHD-Player.exe --instance Pie64',
                        '123\tHD-Player.exe --instance Main\n124\tHD-Player.exe --instance Pie64',
                        '123\tHD-Player.exe --instance Second\n124\tHD-Player.exe --instance Pie64_1']:
            with self.subTest(listing=listing), patch.object(bluestacks, 'find_install', return_value=self.bs), \
                    patch.object(bluestacks.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, listing, '')):
                with self.assertRaises(DeviceError):
                    bluestacks.emulator_identity(DeviceConfig())


    def test_bluestacks_identity_resolves_shown_display_alias(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(bluestacks.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, '123\tHD-Player.exe --instance Main', '')):
            self.assertEqual(bluestacks.emulator_identity(DeviceConfig()), (123,))


    def test_bluestacks_main_display_and_internal_names_use_native_identity(self):
        listing = '123\t"C:\\BlueStacks\\HD-Player.exe"\n124\tHD-Player.exe --instance Pie64_1'
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(bluestacks.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, listing, '')):
            for name in ['', 'Main', 'Pie64', 'Second', 'Pie64_1']:
                self.assertTrue(bool(bluestacks.emulator_identity(DeviceConfig(bluestacks_instance=name))))


    def test_bluestacks_missing_instance_is_unknown(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs):
            with self.assertRaises(DeviceError):
                bluestacks.emulator_identity(DeviceConfig(bluestacks_instance='missing'))


    def test_bluestacks_identity_changes_when_player_is_replaced(self):
        with patch.object(bluestacks, 'find_install', return_value=self.bs), \
                patch.object(bluestacks.subprocess, 'run') as query:
            query.return_value = subprocess.CompletedProcess([], 0, '123\tHD-Player.exe --instance Pie64', '')
            first = bluestacks.emulator_identity(DeviceConfig())
            self.assertEqual(first, (123,))
            query.return_value = subprocess.CompletedProcess([], 0, '124\tHD-Player.exe --instance Pie64', '')
            self.assertNotEqual(bluestacks.emulator_identity(DeviceConfig()), first)



    def test_default_close_keeps_legacy_query_and_running_callback(self):
        with patch.object(bluestacks, 'players', return_value=[(123, 'Pie64')]), \
                patch.object(bluestacks, 'emulator_identity') as strict, \
                patch.object(bluestacks.subprocess, 'run') as kill:
            bluestacks.stop_player(sleep=lambda _: None, running=lambda: False, instance='Pie64')
            strict.assert_not_called()
            self.assertEqual(kill.call_args.args[0], ['taskkill', '/PID', '123', '/F'])

    def test_verified_exit_never_falls_back_to_global_close(self):
        with patch.object(bluestacks.subprocess, 'run') as kill, self.assertRaises(DeviceError):
            bluestacks.stop_player(verify_exit=True)
        kill.assert_not_called()
