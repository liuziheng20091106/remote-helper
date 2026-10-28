"""Check ICC URI boundaries and recent-use behavior with an isolated simulated launcher."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from unittest.mock import patch

from icc_control import IccController, shortcut
from main import Store


def check():
    assert shortcut("ICC://SHOW/")["uri"] == "icc://unfold"
    assert shortcut("icc://page/add")["uri"] == "icc://newpage"
    assert shortcut("icc://ink/lock/start?page=000")["uri"] == "icc://freeze/start?page=0"
    assert shortcut("icc://freeze?page=100")["uri"] == "icc://freeze?page=100"
    scheme = shortcut("icc://config-profile/switch?name=早读+晚自习")
    assert scheme["name"] == "配置方案：早读+晚自习" and "%2B" in scheme["uri"] and "+" not in scheme["uri"]
    assert shortcut("icc://settings/CanvasPage?key=inkFadeSpeedMultiplier")["uri"] == "icc://settings/CanvasPage?key=inkFadeSpeedMultiplier"
    assert shortcut("icc://plugin/MyPlugin/Open?Token=AbC&text=%20hello%20")["uri"] == "icc://plugin/myplugin/open?Token=AbC&text=%20hello%20"
    for bad in ("https://timer", "file:///tmp/a", "javascript:alert(1)", "icc://run", "icc://user@timer", "icc://timer:123",
                "icc://fold?command=exit", "icc://tool/unknown", "icc://freeze?page=true", "icc://freeze?page=-1", "icc://freeze?page=101",
                "icc://freeze?page=1&page=2", "icc://config-profile/switch", "icc://timer#exit", "icc://timer%0a", "icc://timer\n",
                "icc://plugin/../exit", "icc://settings/../exit", "icc://timer?%ZZ=1", "icc://timer\\exit"):
        try:
            shortcut(bad)
            raise AssertionError(f"Unsafe or unsupported URI accepted: {bad!r}")
        except ValueError:
            pass
    with tempfile.TemporaryDirectory(prefix="remote-helper-icc-check-") as directory:
        root = Path(directory)
        store = Store(root)
        controller = IccController(store)
        with patch("icc_control.registered", return_value=True), patch("icc_control.os.startfile"), patch("icc_control.time.time_ns", return_value=1_700_000_000_000_000_000):
            controller.launch("icc://timer")
            controller.launch("icc://show")
            assert [item["uri"] for item in controller.state()["items"][:2]] == ["icc://unfold", "icc://timer"]
            controller.launch("icc://unfold")
            assert set(store.snapshot().icc_recent) == {"icc://unfold", "icc://timer"}
            controller.launch(scheme["uri"])
            assert controller.state()["items"][0]["uri"] == scheme["uri"]
        reloaded = IccController(Store(root))
        assert [item["uri"] for item in reloaded.state()["items"][:3]] == [scheme["uri"], "icc://unfold", "icc://timer"]
        before = store.snapshot().icc_recent
        saved = store.path.read_bytes()
        with patch("icc_control.registered", return_value=False), patch("icc_control.os.startfile", side_effect=AssertionError("Must not open Windows app chooser")):
            try:
                controller.launch("icc://whiteboard")
                raise AssertionError("Missing protocol was accepted")
            except RuntimeError:
                pass
        assert store.snapshot().icc_recent == before and store.path.read_bytes() == saved
        with patch("icc_control.registered", return_value=True), patch("icc_control.os.startfile", side_effect=OSError("simulated dispatch failure")):
            try:
                controller.launch("icc://whiteboard")
                raise AssertionError("Dispatch failure was hidden")
            except RuntimeError:
                pass
        assert store.snapshot().icc_recent == before and store.path.read_bytes() == saved
        with patch("icc_control.registered", return_value=True), patch("icc_control.os.startfile"), patch("icc_control.os.replace", side_effect=OSError("simulated read-only settings")):
            try:
                controller.launch("icc://whiteboard")
                raise AssertionError("Persistence failure was hidden")
            except RuntimeError as error:
                assert "Windows 已接受" in str(error) and "保存失败" in str(error)
        assert store.snapshot().icc_recent == before and store.path.read_bytes() == saved
        with patch("icc_control.registered", return_value=True), patch("icc_control.os.startfile"), patch("icc_control.time.time_ns", return_value=1):
            controller.launch("icc://whiteboard")
            assert controller.state()["items"][0]["uri"] == "icc://whiteboard", "Clock rollback must not bury the latest call"
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(controller.launch, (f"icc://freeze?page={page}" for page in range(12))))
            history = store.snapshot().icc_recent
            assert len(set(history.values())) == len(history), "Concurrent calls must have a total recent-use order"
            assert max(history, key=history.__getitem__) == controller.state()["items"][0]["uri"]
        print("PASS: ICC URI allowlist, aliases, query encoding/case, page bounds, recent-first persistence across reload, failed dispatch/save isolation, clock rollback and concurrent ordering. Native launcher simulated; real settings files isolated.")


if __name__ == "__main__":
    check()
