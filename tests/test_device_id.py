# 端末 ID (未ログイン利用者の識別) の単体テスト
# 実行: python -m unittest discover -s tests
# 要望 (ver7 resolve §2):
#   ・MachineGuid から導出し、ファイルを消しても同じ ID になること
#   ・MachineGuid が読めない場合は保存した乱数を使い、次回も同じ ID になること
#   ・API の検証 (22〜128 文字の [A-Za-z0-9_-]) に収まること
#   ・端末の識別子 (MachineGuid) をそのまま送らないこと
import os
import re
import shutil
import tempfile
import unittest

from src.services import device_id as device_id_module
from src.services.device_id import derive, device_id

# API 側 (DeviceLoginRequest) の検証と同じ条件
_ALLOWED = re.compile(r"^[A-Za-z0-9_-]{22,128}$")


class DeviceIdTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="stretheus-device-test-")
        self._path = os.path.join(self._dir, "device.dat")
        self._original = device_id_module.machine_guid

    def tearDown(self):
        device_id_module.machine_guid = self._original
        shutil.rmtree(self._dir, ignore_errors=True)

    def _with_guid(self, value):
        device_id_module.machine_guid = lambda: value

    def test_derived_id_is_accepted_by_the_api(self):
        self._with_guid("11111111-2222-3333-4444-555555555555")

        self.assertRegex(device_id(self._path), _ALLOWED)

    def test_derived_id_is_stable_without_any_file(self):
        self._with_guid("11111111-2222-3333-4444-555555555555")

        first = device_id(self._path)
        second = device_id(self._path)

        self.assertEqual(first, second)
        # MachineGuid から導出できる限り、保存は要らない (消しても残高が戻らない)。
        self.assertFalse(os.path.exists(self._path))

    def test_derived_id_does_not_leak_the_machine_guid(self):
        guid = "11111111-2222-3333-4444-555555555555"
        self._with_guid(guid)

        self.assertNotIn(guid, device_id(self._path))
        self.assertNotEqual(derive(guid), derive(guid + "x"))

    def test_another_machine_gets_another_id(self):
        self._with_guid("11111111-2222-3333-4444-555555555555")
        first = device_id(self._path)

        self._with_guid("99999999-8888-7777-6666-555555555555")
        second = device_id(self._path)

        self.assertNotEqual(first, second)

    def test_without_the_machine_guid_a_random_id_is_stored(self):
        self._with_guid(None)

        first = device_id(self._path)

        self.assertRegex(first, _ALLOWED)
        self.assertTrue(os.path.isfile(self._path))
        # 2 回目は保存した値を読む (毎回変わると残高が戻ってしまう)。
        self.assertEqual(first, device_id(self._path))

    def test_a_plain_text_file_is_accepted(self):
        # 暗号化できない環境では平文で保存する。次回もその値を読めること。
        self._with_guid(None)
        with open(self._path, "wb") as f:
            f.write(b"stored-device-identifier-0001")

        self.assertEqual("stored-device-identifier-0001", device_id(self._path))


if __name__ == "__main__":
    unittest.main()
