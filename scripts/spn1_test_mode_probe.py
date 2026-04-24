from raspi.main import get_spn1_driver


def main() -> None:
    result = get_spn1_driver().probe_test_mode_entry()
    print("response_R =", repr(result.get("response_R", "")))
    print("response_T =", repr(result.get("response_T", "")))
    print("status =", result.get("status"))


if __name__ == "__main__":
    main()
