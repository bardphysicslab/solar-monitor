from pathlib import Path
import unittest


TEMPLATE = Path("raspi/templates/index.html").read_text(encoding="utf-8")


class SolarNodeTemplateTest(unittest.TestCase):
    def test_single_panel_card_has_one_canonical_reading_set(self):
        for label in ("Panel 1", "Panel 2", "Panel 3", "Panel 4"):
            self.assertNotIn(label, TEMPLATE)
        self.assertIn("function panelReadingValues(data, load)", TEMPLATE)
        self.assertIn('detailRow("Voltage", formatNumber(panelReadings.voltage_v', TEMPLATE)
        self.assertIn('detailRow("Current", formatNumber(panelReadings.current_a', TEMPLATE)
        self.assertIn('detailRow("Power", formatNumber(panelReadings.power_w', TEMPLATE)
        self.assertIn("const measurement = load.panel_reading || {};", TEMPLATE)
        panel_function = TEMPLATE[
            TEMPLATE.index("function panelReadingValues"):
            TEMPLATE.index("function mergeSolarNodes")
        ]
        self.assertNotIn("measurement.voltage_v ?? data.", panel_function)
        self.assertNotIn("measurement.current_a ?? data.", panel_function)
        self.assertNotIn("measurement.power_w ?? data.", panel_function)

    def test_configured_nodes_are_available_before_readings(self):
        self.assertIn("const configuredSolarNodes = {{ configured_wifi_nodes | tojson }};", TEMPLATE)
        self.assertIn("mergeSolarNodes(readings)", TEMPLATE)
        self.assertIn("configuredSolarNodes.map(node => node.uid).filter(Boolean)", TEMPLATE)
        self.assertIn("existing.configured = { ...existing.configured, ...node };", TEMPLATE)
        self.assertIn("nodesByUid.set(node.uid, existing);", TEMPLATE)

    def test_state_badges_are_supported(self):
        for state in ("STOPPED", "WAITING", "LIVE", "OFFLINE", "CATCHUP"):
            self.assertIn(state, TEMPLATE)

    def test_responsive_panel_grid(self):
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
        panels_index = TEMPLATE.index("<summary>Panel Readings</summary>")

        self.assertLess(identity_index, health_index)
        self.assertLess(health_index, panels_index)
        self.assertIn("const openSolarDetails = new Set();", TEMPLATE)
        self.assertIn("data-detail-key", TEMPLATE)
        self.assertIn('detail.addEventListener("toggle"', TEMPLATE)
        self.assertIn("captureSolarDetailOpenState();", TEMPLATE)
        self.assertIn("restoreSolarDetailOpenState();", TEMPLATE)

    def test_load_is_embedded_in_panel_card_and_unassigned_panels_are_supported(self):
        self.assertIn("const configuredLoad = configured.load || null;", TEMPLATE)
        self.assertIn("loadFoldout(uid, configuredLoad, load)", TEMPLATE)
        self.assertIn("No load is explicitly associated with this panel.", TEMPLATE)
        self.assertIn("Electronic Load · ET5406A+", TEMPLATE)
        self.assertNotIn("ET54 Nodes", TEMPLATE)

    def test_selected_mode_is_distinct_from_active_mode_and_activation_is_explicit(self):
        self.assertIn('state.selected_mode === "fixed_resistance"', TEMPLATE)
        self.assertIn('state.active_mode === "fixed_resistance"', TEMPLATE)
        self.assertIn('data-load-action="select-fixed"', TEMPLATE)
        self.assertIn('data-load-action="enable-fixed"', TEMPLATE)
        self.assertIn('data-load-action="apply-fixed"', TEMPLATE)
        self.assertIn('data-load-action="start-sweep"', TEMPLATE)
        self.assertIn('data-load-action="disable"', TEMPLATE)
        self.assertIn('data-load-action="clear-fault"', TEMPLATE)
        self.assertIn("Stop / Disable", TEMPLATE)

    def test_manual_cr_digit_stepper_uses_three_safe_server_validated_steps(self):
        self.assertIn("formatResistanceKohm", TEMPLATE)
        self.assertIn(".toFixed(2)", TEMPLATE)
        self.assertIn("[1000, 100, 10]", TEMPLATE)
        self.assertIn('data-load-action="step"', TEMPLATE)
        self.assertIn("/step`", TEMPLATE)
        self.assertNotIn("4500", TEMPLATE)

    def test_load_scientific_readings_and_safety_state_are_present(self):
        for text in (
            'detailRow("Voltage"',
            'detailRow("Current"',
            'detailRow("Power"',
            'detailRow("Selected resistance"',
            'detailRow("Measured/effective R"',
            'detailRow("Status"',
        ):
            self.assertIn(text, TEMPLATE)

    def test_local_panel_omits_network_identity_while_remote_fields_remain_supported(self):
        self.assertIn('const localSource = configured.source_location === "local";', TEMPLATE)
        self.assertIn('${localSource ? "" : optionalDetailRow("IP address"', TEMPLATE)
        self.assertIn('${localSource ? "" : optionalDetailRow("MAC address"', TEMPLATE)
        self.assertIn('${localSource ? "" : optionalDetailRow("RSSI"', TEMPLATE)
        self.assertIn('${detailRow("Type", "Solar Panel")}', TEMPLATE)
        self.assertIn('${detailRow("UID", uid)}', TEMPLATE)
        self.assertNotIn("<summary>Hardware</summary>", TEMPLATE)

    def test_fixed_and_sweep_controls_are_mode_specific(self):
        self.assertIn("${fixedSelected ? `", TEMPLATE)
        self.assertIn("data-fixed-resistance-controls", TEMPLATE)
        self.assertIn("data-sweep-summary", TEMPLATE)
        self.assertIn('detailRow("Sweep range"', TEMPLATE)
        self.assertIn('detailRow("Settle time"', TEMPLATE)

    def test_invalid_sweep_is_inline_and_cannot_start(self):
        self.assertIn("Boolean(sweepConfig.valid)", TEMPLATE)
        self.assertIn("data-sweep-validation", TEMPLATE)
        self.assertIn('sweepConfig.error || "Sweep configuration is invalid"', TEMPLATE)
        self.assertIn('${canSweep ? "" : "disabled"}>Start Sweep', TEMPLATE)

    def test_latest_sweep_mpp_and_curve_use_raw_points(self):
        for field in ("vmpp_v", "impp_a", "pmpp_w", "rmpp_ohm"):
            self.assertIn(f"lastSweep.{field}", TEMPLATE)
        self.assertIn("const points = Array.isArray(sweep?.points) ? sweep.points : [];", TEMPLATE)
        self.assertIn("plotted.map", TEMPLATE)
        self.assertIn("item.calculatedPower > best.calculatedPower", TEMPLATE)
        self.assertIn("sweep-curve-point-mpp", TEMPLATE)
        self.assertIn("data-sweep-point", TEMPLATE)
        self.assertIn("inspectSweepPoint", TEMPLATE)
        self.assertIn("Resistance (kΩ)", TEMPLATE)
        self.assertIn("Power (W)", TEMPLATE)
        self.assertIn("Math.min(Math.max(x(mpp.resistance) + 8, 70), 300)", TEMPLATE)
        self.assertNotIn('Math.max(y(mpp.power) - 8, 16)', TEMPLATE)

    def test_effective_resistance_requires_enabled_input(self):
        self.assertIn(
            "state.input_enabled ? formatResistance(state.panel_reading?.load_resistance_ohm) : null",
            TEMPLATE,
        )

    def test_poll_failure_is_rendered_separately_from_panel_values(self):
        self.assertIn('detailRow("Poll status", state.poll_status || "Waiting")', TEMPLATE)
        self.assertIn('state.poll_error ? detailRow("Poll error", state.poll_error)', TEMPLATE)
        self.assertIn('detailRow("Transport", state.transport_state || "disconnected")', TEMPLATE)
        self.assertIn('"Unknown (OFF unconfirmed)"', TEMPLATE)

    def test_resistance_format_uses_ohm_symbols(self):
        self.assertIn("function formatResistance(valueOhm)", TEMPLATE)
        self.assertIn("kΩ", TEMPLATE)
        self.assertIn(" Ω`", TEMPLATE)

    def test_sweep_run_modes_and_timing_statistics_are_rendered(self):
        self.assertIn('data-load-action="select-single-shot"', TEMPLATE)
        self.assertIn('data-load-action="select-continuous"', TEMPLATE)
        self.assertIn('payload.run_mode = action === "select-continuous" ? "continuous" : "single_shot"', TEMPLATE)
        self.assertIn("data-continuous-timing", TEMPLATE)
        for label in ("Interval", "Last", "Min", "Max", "Mean", "Median", "P95", "Overruns", "Skipped boundaries"):
            self.assertIn(f'detailRow("{label}"', TEMPLATE)
        self.assertIn('detailRow("Electrical sweep"', TEMPLATE)
        self.assertIn('detailRow("Irradiance"', TEMPLATE)

    def test_rendering_and_mode_selection_do_not_activate_load(self):
        select_start = TEMPLATE.index('if (action === "select-fixed" || action === "select-sweep")')
        select_end = TEMPLATE.index('} else if (action === "step")', select_start)
        self.assertNotIn("enable", TEMPLATE[select_start:select_end].lower())
        self.assertNotIn("input", TEMPLATE[select_start:select_end].lower())


if __name__ == "__main__":
    unittest.main()
