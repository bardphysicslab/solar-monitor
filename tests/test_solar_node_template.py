from pathlib import Path
import unittest


TEMPLATE = Path("raspi/templates/index.html").read_text(encoding="utf-8")


class SolarNodeTemplateTest(unittest.TestCase):
    def test_current_dummy_voltage_maps_to_panel_one(self):
        self.assertIn("data.panel_voltage_1_v ?? data.panel_voltage_v ?? null", TEMPLATE)
        self.assertIn("data.panel_voltage_2_v ?? null", TEMPLATE)
        self.assertIn("data.panel_voltage_3_v ?? null", TEMPLATE)
        self.assertIn("data.panel_voltage_4_v ?? null", TEMPLATE)

    def test_configured_nodes_are_available_before_readings(self):
        self.assertIn("const configuredSolarNodes = {{ configured_wifi_nodes | tojson }};", TEMPLATE)
        self.assertIn("mergeSolarNodes(readings)", TEMPLATE)
        self.assertIn('"bb-solar-pnl-001"', TEMPLATE)
        self.assertIn('"bb-solar-pnl-002"', TEMPLATE)
        self.assertIn('"bb-solar-pnl-003"', TEMPLATE)
        self.assertIn('"bb-solar-pnl-004"', TEMPLATE)
        self.assertIn("existing.configured = { ...existing.configured, ...node };", TEMPLATE)
        self.assertIn("nodesByUid.set(node.uid, existing);", TEMPLATE)

    def test_state_badges_are_supported(self):
        for state in ("STOPPED", "WAITING", "LIVE", "OFFLINE", "CATCHUP"):
            self.assertIn(state, TEMPLATE)

    def test_responsive_four_three_two_one_grid(self):
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr));", TEMPLATE)
        self.assertIn("@media (max-width: 1200px)", TEMPLATE)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", TEMPLATE)
        self.assertIn("@media (max-width: 900px)", TEMPLATE)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", TEMPLATE)
        self.assertIn("@media (max-width: 600px)", TEMPLATE)
        self.assertIn("grid-template-columns: 1fr;", TEMPLATE)

    def test_spn1_control_and_chart_ids_remain(self):
        for element_id in (
            "start-button",
            "stop-button",
            "total-chart",
            "diffuse-chart",
            "sun-chart",
            "total-value",
            "diffuse-value",
            "sun-value",
        ):
            self.assertIn(f'id="{element_id}"', TEMPLATE)

    def test_manual_spn1_time_card_is_removed(self):
        self.assertNotIn("SPN1 Time", TEMPLATE)
        self.assertNotIn("spn1-time-sync", TEMPLATE)
        self.assertNotIn("Sync to Server Time", TEMPLATE)

    def test_foldout_order_and_persistence_hooks_exist(self):
        identity_index = TEMPLATE.index("<summary>Identity</summary>")
        health_index = TEMPLATE.index("<summary>Health</summary>")
        hardware_index = TEMPLATE.index("<summary>Hardware</summary>")
        panels_index = TEMPLATE.index("<summary>Panel Readings</summary>")

        self.assertLess(identity_index, health_index)
        self.assertLess(health_index, hardware_index)
        self.assertLess(hardware_index, panels_index)
        self.assertIn("const openSolarDetails = new Set();", TEMPLATE)
        self.assertIn("data-detail-key", TEMPLATE)
        self.assertIn('detail.addEventListener("toggle"', TEMPLATE)
        self.assertIn("captureSolarDetailOpenState();", TEMPLATE)
        self.assertIn("restoreSolarDetailOpenState();", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
