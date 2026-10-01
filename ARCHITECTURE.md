# solar-monitor Architecture

## Current structure
- `raspi/load_control.py`: `ElectronicLoadController` owns the electronic-load
  safety envelope, OFF confirmation, fault latching, mode state and sweep
  sequencing. It receives its driver (`ElectronicLoadDriver` Protocol) and
  `sleep_fn` / `monotonic_fn` / `wait_fn` as constructor arguments.
- `raspi/drivers/et54_driver.py`: translates controller commands to the
  ET5406A+ and reports the instrument's hardware limits; it holds no safety
  policy.
- `raspi/main.py`: configuration, polling threads, SPN1 time-sync policy and
  routes; load routes translate requests into controller calls.
- `docs/mathematical-methods.md`: sweep and safety formulas; code and tests
  must agree with it.

## Constraint
Safety checks stay in the controller, not in routes, UI or drivers.
