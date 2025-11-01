# vesp/dbus_mesh.py
from __future__ import annotations
from typing import Dict, Any, Optional, Tuple
from gi.repository import GLib

APP_ROOT   = "/org/vespulaa/app"
AGENT_PATH = "/org/vespulaa/app/agent"
ELEM0_PATH = "/org/vespulaa/app/ele00"

# ---------- Introspection XML (no f-strings!) ----------
APP_XML = """
<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects">
      <arg name="objects" type="a{oa{sa{sv}}}" direction="out"/>
    </method>
  </interface>

  <interface name="org.bluez.mesh.Application1">
    <property name="CompanyID" type="q" access="read"/>
    <property name="ProductID" type="q" access="read"/>
    <property name="VersionID" type="q" access="read"/>
    <property name="CRPL" type="q" access="read"/>
    <property name="Elements" type="as" access="read"/>
    <method name="JoinComplete">
      <arg name="token" type="t" direction="in"/>
    </method>
    <method name="JoinFailed">
      <arg name="reason" type="s" direction="in"/>
    </method>
  </interface>

  <!-- Provisioner1 lives on the SAME object as Application1 -->
  <interface name="org.bluez.mesh.Provisioner1">
    <method name="ScanResult">
      <arg name="rssi" type="n" direction="in"/>
      <arg name="data" type="ay" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
    </method>
    <method name="RequestProvData">
      <arg name="count" type="y" direction="in"/>
      <arg name="net_index" type="q" direction="out"/>
      <arg name="unicast"   type="q" direction="out"/>
    </method>
    <method name="AddNodeComplete">
      <arg name="uuid"    type="ay" direction="in"/>
      <arg name="unicast" type="q"  direction="in"/>
      <arg name="count"   type="y"  direction="in"/>
    </method>
    <method name="AddNodeFailed">
      <arg name="uuid"   type="ay" direction="in"/>
      <arg name="reason" type="s"  direction="in"/>
    </method>
  </interface>
</node>
"""

ELEMENT_XML = """
<node>
  <interface name="org.bluez.mesh.Element1">
    <method name="MessageReceived">
      <arg name="source"      type="q"  direction="in"/>
      <arg name="key_index"   type="q"  direction="in"/>
      <arg name="destination" type="v"  direction="in"/>
      <arg name="data"        type="ay" direction="in"/>
    </method>
    <method name="DevKeyMessageReceived">
      <arg name="source"    type="q"  direction="in"/>
      <arg name="remote"    type="b"  direction="in"/>
      <arg name="net_index" type="q"  direction="in"/>
      <arg name="data"      type="ay" direction="in"/>
    </method>
    <property name="Index"        type="y"          access="read"/>
    <property name="Models"       type="a(qa{sv})"  access="read"/>
    <property name="VendorModels" type="a(qqa{sv})" access="read"/>
    <property name="Location"     type="q"          access="read"/>
  </interface>
</node>
"""

AGENT_XML = """
<node>
  <interface name="org.bluez.mesh.ProvisionAgent1">
    <method name="Capabilities">
      <arg name="caps" type="a{sv}" direction="out"/>
    </method>
    <method name="OutNumber">
      <arg name="action" type="y" direction="in"/>
      <arg name="number" type="u" direction="in"/>
    </method>
    <method name="OutString">
      <arg name="string" type="s" direction="in"/>
    </method>
    <method name="DisplayNumber">
      <arg name="action" type="y" direction="in"/>
      <arg name="number" type="u" direction="in"/>
    </method>
    <method name="DisplayString">
      <arg name="string" type="s" direction="in"/>
    </method>
    <method name="PromptNumeric">
      <arg name="action"  type="y" direction="in"/>
      <arg name="maximum" type="u" direction="in"/>
      <arg name="number"  type="u" direction="out"/>
    </method>
    <method name="PromptStatic">
      <arg name="hex" type="ay" direction="out"/>
    </method>
    <method name="Cancel"/>
  </interface>
</node>
"""

# ---------- Implementations ----------

class AppRoot:
    """
    /org/vesp/app:
      - org.freedesktop.DBus.ObjectManager
      - org.bluez.mesh.Application1
      - org.bluez.mesh.Provisioner1
    """
    __dbus_xml__ = APP_XML
    controller = None  # injected

    # ObjectManager
    def GetManagedObjects(self) -> Dict[str, Dict[str, Dict[str, GLib.Variant]]]:
        elem_props: Dict[str, GLib.Variant] = {
            "Index":        GLib.Variant('y', 0),       # uint8
            "Location":     GLib.Variant('q', 0x0000),  # uint16 (primary)
            "Models": GLib.Variant('a(qa{sv})', [
                (0x0000, {}),  # Configuration Server
                (0x0001, {}),  # Configuration Client
                (0x0002, {}),  # Health Server
                (0x0004, {}),  # Remote Provisioning Server
                (0x0005, {}),  # Remote Provisioning Client
                (0x0008, {}),  # Private Beacon Server
                (0x1001, {}),  # Generic OnOff Client
            ]),
            "VendorModels": GLib.Variant('a(qqa{sv})', []),
        }

        app_props: Dict[str, GLib.Variant] = {
            "CompanyID": GLib.Variant('q', 0x02E5),  # Espressif (matches your logs)
            "ProductID": GLib.Variant('q', 0x0000),
            "VersionID": GLib.Variant('q', 0x0001),
            "CRPL":      GLib.Variant('q', 0x000A),
            "Elements":  GLib.Variant('as', [ELEM0_PATH]),
        }

        return {
            APP_ROOT: {
                "org.bluez.mesh.Application1": app_props,
                "org.bluez.mesh.Provisioner1": {},
            },
            ELEM0_PATH: {
                "org.bluez.mesh.Element1": elem_props,
            },
            AGENT_PATH: {
                "org.bluez.mesh.ProvisionAgent1": {},
            },
        }

    # Application1 callbacks from meshd
    def JoinComplete(self, token: int):
        if self.controller:
            self.controller.on_join_complete(int(token))

    def JoinFailed(self, reason: str):
        if self.controller:
            self.controller.on_join_failed(str(reason))

    # Provisioner1 callbacks from meshd
    def ScanResult(self, rssi: int, data: bytes, options: Dict[str, GLib.Variant]):
        if self.controller:
            self.controller.on_scan_result(int(rssi), bytes(data), dict(options) if options else None)

    def RequestProvData(self, count: int) -> Tuple[int, int]:
        if self.controller:
            start = self.controller.alloc_unicast(int(count))
            return (0, start)  # net_index=0
        return (0, 0x0005)

    def AddNodeComplete(self, uuid: bytes, unicast: int, count: int):
        if self.controller:
            self.controller.on_add_node_complete(bytes(uuid), int(unicast), int(count))

    def AddNodeFailed(self, uuid: bytes, reason: str):
        if self.controller:
            self.controller.on_add_node_failed(bytes(uuid), str(reason))


class Element0:
    __dbus_xml__ = ELEMENT_XML
    controller = None

    def MessageReceived(self, source: int, key_index: int, destination: GLib.Variant, data: bytes):
        if self.controller:
            self.controller.on_element_message(int(source), int(key_index), destination, bytes(data))

    def DevKeyMessageReceived(self, source: int, remote: bool, net_index: int, data: bytes):
        if self.controller:
            self.controller.on_element_devkey_message(int(source), bool(remote), int(net_index), bytes(data))


class ProvisionAgent:
    __dbus_xml__ = AGENT_XML
    controller = None

    def Capabilities(self) -> Dict[str, GLib.Variant]:
        # Minimal No-OOB
        return {
            "StaticOOB": GLib.Variant('b', False),
            "OutputOOB": GLib.Variant('b', False),
            "InputOOB":  GLib.Variant('b', False),
            "OOBType":   GLib.Variant('s', "none"),
        }

    # No-OOB paths unused:
    def OutNumber(self, action: int, number: int): pass
    def OutString(self, string: str): pass
    def DisplayNumber(self, action: int, number: int): pass
    def DisplayString(self, string: str): pass
    def PromptNumeric(self, action: int, maximum: int) -> int: return 0
    def PromptStatic(self) -> bytes: return b""
    def Cancel(self): pass


def build_object_manager_map(app: AppRoot, agent: ProvisionAgent, elem0: Element0):
    # Controller.export() just logs something; meshd will call GetManagedObjects() itself.
    return {APP_ROOT: {}, AGENT_PATH: {}, ELEM0_PATH: {}}

