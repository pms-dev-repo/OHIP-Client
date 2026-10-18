"""Interactive diagnostics. No requests occur until a menu test is selected."""
from datetime import date
import argparse
from config import Config, ConfigurationError, PROFILE_LABELS
from ohip_client import OHIPClient, OHIPError


def reservation_summary(data):
    container = data.get("reservations", {})
    if not isinstance(container, dict):
        raise OHIPError("Unexpected reservation structure; check environment API version.")
    rows = container.get("reservationInfo", container.get("reservation", []))
    if not isinstance(rows, list):
        print("Unexpected reservation structure; check environment API version.")
        return
    print(f"Reservations returned: {len(rows)}")
    for row in rows[:10]:
        ids = row.get("reservationIdList", [])
        rid = next((i.get("id") for i in ids if i.get("type") == "Reservation"), "(unavailable)")
        stay = row.get("roomStay", {})
        print(f"ID: {rid} | Status: {row.get('reservationStatus', '(unavailable)')} | "
              f"Arrival: {stay.get('arrivalDate', '(unavailable)')} | "
              f"Departure: {stay.get('departureDate', '(unavailable)')}")


def profile_summary(data):
    container = data.get("profileSummaries", {})
    rows = container.get("profileInfo", []) if isinstance(container, dict) else []
    if not isinstance(rows, list):
        raise OHIPError("Unexpected profile structure; check environment API version.")
    print(f"Profiles returned: {len(rows)}")
    for row in rows[:10]:
        ids = row.get("profileIdList", [])
        print("Profile IDs: " + ", ".join(str(i.get("id", "")) for i in ids if i.get("type") == "Profile"))
    print("Names, contact details and other personal information are omitted.")


def main(argv=()):
    parser = argparse.ArgumentParser(description="Local read-only OHIP diagnostics")
    parser.add_argument("--hotel", choices=PROFILE_LABELS, default="legacy")
    args = parser.parse_args(argv)
    try:
        client = OHIPClient(Config.from_env(args.hotel))
    except ConfigurationError as exc:
        print(exc)
        return
    try:
        while True:
            print(f"""
========================================
 PMS CONSULTING & SUPPORT
 ORACLE OHIP TEST CLIENT
========================================
 Hotel: {PROFILE_LABELS[client.config.profile]}
 Environment: {client.config.environment or 'Configured OHIP Environment'}

 1. Test Authentication
 2. Test API Connectivity
 3. Search Reservations
 4. Get Reservation Details
 5. Search Guest Profiles
 6. View Configuration Status
 0. Exit
""")
            choice = input("Select an option: ").strip()
            if choice == "0":
                break
            try:
                if choice == "1":
                    client.authenticate()
                    print(f"Authentication: SUCCESS\nToken received: YES\nToken expires in: {client.expires_in} seconds")
                elif choice == "2":
                    status, message = client.test_connection()
                    print(f"HTTP status: {status}\n{message}")
                elif choice == "3":
                    params = {}
                    for key, label in [("arrivalStartDate", "Arrival start YYYY-MM-DD"),
                                       ("arrivalEndDate", "Arrival end YYYY-MM-DD"),
                                       ("surname", "Surname"),
                                       ("confirmationNumberList", "Confirmation number")]:
                        value = input(label + " (blank to skip): ").strip()
                        if value:
                            if key.startswith("arrival"):
                                date.fromisoformat(value)
                            params[key] = value
                    reservation_summary(client.search_reservations(params))
                elif choice == "4":
                    reservation_summary(client.reservation_details(input("Reservation ID: ")))
                elif choice == "5":
                    profile_summary(client.search_profiles(input("Guest profile name: ")))
                elif choice == "6":
                    for key, value in client.config.status().items():
                        print(f"{key}: {value}")
                else:
                    print("Select a listed option.")
            except (ConfigurationError, OHIPError) as exc:
                print(f"Diagnostic: {exc}")
            except ValueError:
                print("Invalid input; use dates in YYYY-MM-DD format.")
    except (EOFError, KeyboardInterrupt):
        print("\nExiting.")
    finally:
        client.close()


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
