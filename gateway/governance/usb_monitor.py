"""USB storage detection + block (Windows). No-op without `wmi`/`winreg`."""

from __future__ import annotations

import logging

log = logging.getLogger("gateway.usb")


class UsbMonitor:
    def __init__(self, policy: dict) -> None:
        self.policy = policy or {}
        self._wmi = None
        try:
            import wmi  # type: ignore

            self._wmi = wmi.WMI()
        except Exception as exc:  # ImportError, COM error, non-Windows
            log.info("USB monitor inactive (wmi unavailable): %s", exc)

    def poll(self) -> list[dict]:
        """Violations for USB mass-storage devices currently attached."""
        if self._wmi is None:
            return []
        out: list[dict] = []
        try:
            for drive in self._wmi.Win32_DiskDrive():
                if (getattr(drive, "InterfaceType", "") or "").upper() == "USB":
                    out.append({
                        "type": "USB_INSERTED",
                        "severity": "warning",
                        "summary": f"USB storage attached: {getattr(drive, 'Model', 'unknown')}",
                        "detail": {"model": getattr(drive, "Model", None), "serial": getattr(drive, "SerialNumber", None)},
                    })
        except Exception as exc:
            log.debug("usb poll error: %s", exc)
        return out

    def enforce_block(self) -> None:
        """Disable USB storage via registry policy (needs admin). Best-effort;
        the durable control is a domain GPO."""
        if not self.policy.get("block_usb"):
            return
        try:
            import winreg  # type: ignore

            key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\USBSTOR")
            winreg.SetValueEx(key, "Start", 0, winreg.REG_DWORD, 4)  # 4 = disabled
            winreg.CloseKey(key)
            log.info("USB storage disabled via registry policy")
        except Exception as exc:
            log.warning("USB block not applied (admin? non-Windows?): %s", exc)
