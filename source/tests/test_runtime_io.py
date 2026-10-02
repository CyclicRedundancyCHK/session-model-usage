import ctypes
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from session_model_usage.runtime_io import atomic_json, read_json, ProcessLock
from session_model_usage.diagnostics import record


class RuntimeIOTests(unittest.TestCase):
    def test_short_permission_failure_retries(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'runtime.json'; original=os.replace; attempts=[]
            def replace(a,b):
                attempts.append(1)
                if len(attempts)<3: raise PermissionError('fixture')
                original(a,b)
            with patch('session_model_usage.runtime_io.os.replace',side_effect=replace):
                self.assertTrue(atomic_json(path,{'status':'connected'},delay=0))
            self.assertEqual(read_json(path)['status'],'connected')
            self.assertEqual(len(attempts),3)

    def test_permanent_write_failure_is_bounded_and_preserves_previous(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'runtime.json'; atomic_json(path,{'status':'previous'})
            with patch('session_model_usage.runtime_io.os.replace',side_effect=PermissionError('fixture')) as replace:
                self.assertFalse(atomic_json(path,{'status':'new'},delay=0))
            self.assertEqual(replace.call_count,4)
            self.assertEqual(read_json(path)['status'],'previous')
            self.assertFalse(list(Path(d).glob('*.tmp')))

    def test_partial_json_is_not_used(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'runtime.json'; p.write_text('{"status":')
            self.assertEqual(read_json(p),{})

    def test_non_object_json_is_not_used(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'runtime.json'; p.write_text('[]')
            self.assertEqual(read_json(p),{})

    @unittest.skipUnless(os.name=='nt','native Windows sharing')
    def test_native_shared_reader_does_not_raise_or_destroy_state(self):
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
        kernel.CreateFileW.restype=ctypes.c_void_p
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'runtime.json'; atomic_json(path,{'status':'previous'})
            handle=kernel.CreateFileW(str(path),0x80000000,3,None,3,0x80,None)
            self.assertNotIn(handle,(None,ctypes.c_void_p(-1).value))
            try:
                self.assertFalse(atomic_json(path,{'status':'new'}))
                self.assertEqual(read_json(path)['status'],'previous')
            finally: kernel.CloseHandle(handle)
            self.assertTrue(atomic_json(path,{'status':'new'}))

    @unittest.skipUnless(os.name=='nt','process mutex')
    def test_only_one_lifetime_lock_holder(self):
        with tempfile.TemporaryDirectory() as d:
            one,two=ProcessLock(Path(d)/'supervisor.lock'),ProcessLock(Path(d)/'supervisor.lock')
            try:
                self.assertTrue(one.acquire()); self.assertFalse(two.acquire())
                one.close(); self.assertTrue(two.acquire())
            finally: one.close(); two.close()

    def test_diagnostics_excludes_exception_values_and_conversation_text(self):
        with tempfile.TemporaryDirectory() as d:
            record('fixture',RuntimeError('private chat content and credential'),folder=Path(d))
            text=(Path(d)/'diagnostics.log').read_text()
            self.assertNotIn('private chat',text); self.assertNotIn('credential',text)
            self.assertIn('RuntimeError',text)


if __name__=='__main__': unittest.main()
