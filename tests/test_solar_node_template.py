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
        self.assertIn("nodesByUid.set(node.uid, { configured: node, reading: null });", TEMPLATE)

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
            "spn1-time-sync",
            "total-chart",
            "diffuse-chart",
            "sun-chart",
            "total-value",
            "diffuse-value",
            "sun-value",
        ):
            self.assertIn(f'id="{element_id}"', TEMPLATE)


if __name__ == "__main__":
    unittest.main()
