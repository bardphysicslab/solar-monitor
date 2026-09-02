# Solar Monitor mathematical methods

This is the canonical inventory of measured, configured, and calculated quantities in Solar Monitor. It describes the implementation as audited on 2026-09-02. “Measured” means reported by an instrument or remote node; it does not imply an independent calibration. Items under **Known gaps** are requirements that the present implementation does not yet satisfy.

## Time and acquisition model

SPN1 and ET54 acquisition are independent background loops. Both normally poll at approximately 1 Hz. A load safety fault switches the ET54 input off and latches the fault but does not stop read-only acquisition. The live UI uses the most recent successful ET54 reading as one atomic four-channel object. If a later attempt fails, the values are retained and marked `STALE`; `WAITING` means no successful reading exists. A sweep temporarily owns ET54 serial access, so ordinary ET54 polling pauses during a sweep.

Driver timestamps are UTC strings generated after a response has been parsed; they are not instrument exposure-start times. The SPN1 sample cache compares these timestamp strings inclusively when selecting `start <= timestamp <= end`.

Configured recording windows are elapsed-monotonic-time windows, normally 10 seconds. A window starts when its first accepted sample arrives and ends when a later add/flush observes that its interval is due. Thus windows are not aligned to wall-clock ten-second boundaries. `window_start_utc` and `window_end_utc` are application times at opening and completion; `timestamp_utc` is the completion time. Only readings with `status == "ok"` enter an average.

For a numeric channel with valid finite samples \(x_1,\ldots,x_n\), the recorder writes

$$\bar{x}=\frac{1}{n}\sum_{i=1}^{n}x_i.$$

Invalid, nonnumeric, Boolean, NaN, and infinite values are omitted per channel. An empty channel is written blank. `sample_count` counts accepted reading objects and can exceed a channel's valid count. Binary channels use majority vote; a tie uses the latest valid binary sample.

## SPN1 solar radiation

| Quantity | Classification | Inputs and implementation | Live/recorded behavior |
|---|---|---|---|
| `total_w_m2` | Directly reported by SPN1 | First numeric field of the parsed `S` response, W/m² | Live value is the latest successful ~1 Hz reading. CSV is the arithmetic mean of valid samples in the recording window. |
| `diffuse_w_m2` | Directly reported by SPN1 | Second numeric field of the parsed `S` response, W/m² | Same timing and missing-data rules as total radiation. |
| `sun` | Directly reported categorical instrument result | Third `S` field, parsed only as 0 or 1. Solar Monitor does not derive it from total/diffuse radiation. | Live UI maps 1 to “Sun” and 0 to “No sun”. CSV uses majority; ties use the latest sample. |

The parser accepts an optional leading `S`, whitespace, signed decimal radiation values, and a final 0/1. It retries once after an unparseable response. Failed readings are returned with error status and do not replace the displayed SPN1 values or enter recording averages.

Solar Monitor does **not** calculate direct-beam or direct-normal radiation (for example, `total - diffuse`). It also does not transform SPN1 total radiation into plane-of-array irradiance. The SPN1 and panel geometry is not configured, so SPN1 total radiation must not presently drive an irradiance-informed panel MPP estimate.

During a sweep, cached SPN1 `total_w_m2` samples whose application timestamps fall inclusively between actual sweep start and completion are summarized as mean, minimum, maximum, population standard deviation,

$$\sigma=\sqrt{\frac{1}{n}\sum_{i=1}^{n}(G_i-\bar G)^2},$$

and count. With one point, standard deviation is zero; with none, all statistics are `None`. Irradiance quality is `unavailable` for no samples, or `unstable` when configured standard-deviation or relative-range thresholds are exceeded. Relative range is `(max-min)/mean`; a nonpositive mean is unstable. These statistics qualify a sweep but do not change its electrical MPP selection.

ET54 ten-second CSV rows independently attach SPN1 statistics selected over the ET54 recording window using the same inclusive timestamp test. This is temporal association, not proof that SPN1 radiation equals panel-plane irradiance.

## Panel electrical quantities

ET54 `MEAS:ALL?` returns four whitespace-separated values in the order current, voltage, power, resistance. Solar Monitor parses and exposes them as `current_a`, `voltage_v`, `power_w`, and `load_resistance_ohm`. All four are **instrument-reported measurements**. The normal live path does not replace ET54 power with \(VI\), and it does not calculate live resistance as \(V/I\).

The ET54's unavailable-resistance sentinel (at least 99,999,999 Ω) becomes `None`. Consequently an open/off operating point displays `Open` in Panel Readings rather than the previous setpoint. Zero current does not cause Solar Monitor to divide by zero because live effective resistance is not calculated locally. Failed or malformed ET54 readings have error status and do not replace the last successful atomic reading.

Ten-second ET54 CSV values are independent per-channel arithmetic means of valid instrument-reported samples. In particular, mean power is the mean of reported power, not the product of mean voltage and mean current; mean resistance is the mean of reported resistance, not the ratio of means.

`resistance_setpoint_ohm` is commanded/configured load state, not measured effective resistance. It is recorded as the mean of numeric setpoint provenance values in the window. `active_mode` and `safety_state` use the latest accepted sample's strings.

## Datasheet and MPP quantities

`panel_spec` is manufacturer/reference metadata. Nominal MPP resistance is calculated when both inputs are positive finite numbers:

$$R_{MPP,nom}=\frac{V_{MPP,STC}}{I_{MPP,STC}}.$$

It is `None` otherwise. For the current metadata, 7.28 V / 0.330 A = 22.060606… Ω. This is a search/startup setpoint, not a measurement. Selecting it never enables the load.

Sweeps preserve every ET54 point with timestamp, phase, resistance setpoint, and instrument-reported V, I, P, and effective R. MPP selection maximizes the ET54-reported measured power:

$$j=\operatorname*{arg\,max}_i P_{ET54,i}.$$

The measured summary is `vmpp_v = V_j`, `impp_a = I_j`, `pmpp_w = P_ET54,j`, and `rmpp_ohm` equal to the ET54-reported effective resistance at point \(j\). No estimate is substituted. Only a successfully bracketed, completed sweep updates remembered measured RMPP; aborted or unbracketed sweeps do not.

## Adaptive sweep algorithm

The center hierarchy is: the most recent valid measured RMPP; otherwise a plane-of-array irradiance estimate only when configuration explicitly says the SPN1 represents panel-plane irradiance; otherwise nominal datasheet RMPP. The current configuration does not assert that geometry, so it cannot select the irradiance path. For valid POA use, positive samples from the configured lookback (default 60 seconds) are averaged and used in the documented first-order estimator below.

A normal sweep takes eight logarithmically spaced first-pass measurements from `max(safe minimum, 0.5 center)` through `min(safe maximum, 2 center)`. The center must lie strictly inside those bounds. The highest ET54-reported-power first-pass point is bracketed only if it has measured neighbors on both resistance sides. Four new logarithmic points strictly inside those neighboring bounds form the refinement pass, producing 12 total measurements.

If the first-pass maximum is at an edge, the four refinement measurements are not taken. Instead, `automatic_sweep_values` performs a bounded broad recovery over the complete safe range. For requested recovery count \(N\), it computes

$$q=(R_{max}/R_{min})^{1/(N-2)},$$

generates \(R_{max}/q^k\), adds the minimum and center, deduplicates rounded values, and sorts descending. Recovery succeeds only when its maximum has neighbors on both sides. If its maximum is also at an edge, status is `unbracketed`, MPP summary values are `None`, and measured RMPP is not updated. Every proposed value is validated; values are never silently clamped. Each point is applied, allowed to settle for `settle_s`, then measured. Input is explicitly turned off afterward.

Explicit `resistance_values_ohm` remains a legacy/operator-defined override. Such values are validated and preserved but do not receive automatic refinement; the normal active configuration does not use this override.

A sweep's `actual_started_at` is set before instrument preparation; point timestamps are generated after each measurement. `completed_at` follows the final OFF attempt. Duration uses a monotonic clock. Continuous runs are scheduled from a fixed monotonic origin; missed boundaries are skipped and counted. Duration statistics are minimum, maximum, arithmetic mean, median, nearest-rank 95th percentile (`ceil(0.95 n)-1` in zero-based sorted data), overruns, and skipped boundaries.

The irradiance-only first-order estimator, when valid plane-of-array geometry exists, is

$$I_{MPP,est}\approx I_{MPP,STC}\frac{G}{G_{STC}},\qquad
R_{MPP,est}\approx\frac{V_{MPP,STC}}{I_{MPP,STC}(G/G_{STC})}.$$

It is used only to choose a search region. Actual MPP always comes from ET54 measurements. Sweep JSON preserves all raw points plus center resistance/source, estimated RMPP, initial bounds, first-pass and refinement values, bracket status, recovery use/values, irradiance used, and a reserved module-temperature value. The daily summary CSV carries the principal provenance and outcome fields; complete reconstruction uses the JSON.

## Safety quantities and provenance

`Voc`, `Isc`, `Vmp`, `Imp`, `Pmp`, and power tolerance are manufacturer characterization/reference values, not automatically software trips. Solar Monitor no longer derives a forward-current trip from `Isc × (1 + safety margin)`, and `safety_margin_pct` is not part of `panel_spec`.

The current safety envelope takes the minimum configured voltage/current/power maxima across panel absolute, panel operating, ET54 absolute, and ET54 operating profiles. For a source explicitly declared `current_limited`, source V/I/P capability must not exceed the configured ET54 limits, and the resistance minimum comes from explicit load/mode/hardware bounds rather than stiff-source \(V/I\) or \(V^2/P\). For other sources, those two conservative resistance floors are included. Resistance is separately checked against the ET5406A+ 0.05–4500 Ω hardware range. Runtime V, I, and P trip on `value >= configured limit`; missing values trip while the load is active.

**Unresolved provenance:** the active 0.36 A configured limit remains in place after removal of the invalid Isc-derived interpretation. The repository does not establish an independent wiring/connector/fixture/experimental basis for that number. It must not be described as an Isc-derived panel hard limit; an applicable deliberately selected test-system limit should replace or validate it.

Manufacturer maximum system voltage, source-circuit fuse ratings, and limiting reverse current are not currently represented as distinct fields. If added, they must retain those exact meanings and must not automatically feed forward-load runtime limits.

## Module temperature

`module_temperature_c` is reserved as a first-class numeric panel channel and Solar Monitor can preserve and mean-aggregate it in the panel CSV. Panel configuration identifies the channel and optional sensor UID; it is disabled while no module/backsheet sensor is installed. It is not populated by the current hardware, is not included in sweep context yet, and has no panel-specific temperature-coefficient model. Ambient temperature is not substituted. No temperature correction is applied to nominal or measured MPP. Datasheet coefficients must be stored with their exact quantity and units; no VMPP coefficient may be inferred from Voc, Isc, or Pmp coefficients without a documented panel-specific model.

## Other transformations

- UI formatting rounds voltage/current/power to three decimal places, panel metadata according to its display formatter, resistance below 1000 Ω to one decimal place, and resistance at or above 1000 Ω to two decimal kΩ. This is display-only.
- Resistance arrow edits add or subtract 0.1, 1, 10, 100, or 1000 Ω and round to six decimal places. Crossing configured bounds raises an error; there is no wrapping or clamping.
- Recorder irradiance and numeric sensor values are serialized to three decimal places. Blank means no valid sample.
- Wi-Fi-node CSV values are parsed/type-converted but panel voltages, RSSI, and `voltage_ok` are not mathematically transformed before recording. `voltage_ok` is majority-aggregated as a binary value.
- Sweep IDs are UUIDs. Daily file selection uses the UTC date of window/sweep completion.
- Example-driver sine-wave values are synthetic demonstration data and are not used by configured Solar Monitor hardware.
