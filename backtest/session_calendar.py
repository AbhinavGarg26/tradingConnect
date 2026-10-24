"""Published closures for the regular NSE session, supplementing the DB calendar.

2020: https://nsearchives.nseindia.com/content/circulars/FAOP42878.pdf
2021: https://archives.nseindia.com/content/circulars/CMTR46623.pdf
2022: https://archives.nseindia.com/content/circulars/FAOP50561.pdf
2023: https://nsearchives.nseindia.com/content/circulars/CMTR54757.pdf
2023 Bakri Id amended to Jun 29: https://archives.nseindia.com/content/circulars/CMPT57291.pdf
2024: https://archives.nseindia.com/content/circulars/FAOP59723.pdf
2024 additions: CMTR60338.zip (Jan 22), FAOP61517.pdf (May 20),
FAOP64959.pdf (Nov 20), under https://nsearchives.nseindia.com/content/circulars/
2025: https://nsearchives.nseindia.com/content/circulars/FAOP65588.pdf
2026: https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf
Additional closure: https://nsearchives.nseindia.com/content/circulars/FAOP72262.pdf
Muhurat sessions are not regular 09:15–15:30 sessions and are excluded.
Additional/ad-hoc closures must still be recorded in trading_holidays.
This fallback is backtest-only; it does not change the live service calendar.
"""
from datetime import date


REGULAR_SESSION_HOLIDAYS = frozenset(date.fromisoformat(value) for value in (
    '2020-02-21', '2020-03-10', '2020-04-02', '2020-04-06', '2020-04-10', '2020-04-14', '2020-05-01', '2020-05-25', '2020-10-02', '2020-11-16', '2020-11-30', '2020-12-25',
    '2021-01-26', '2021-03-11', '2021-03-29', '2021-04-02', '2021-04-14', '2021-04-21', '2021-05-13', '2021-07-21', '2021-08-19', '2021-09-10', '2021-10-15', '2021-11-04', '2021-11-05', '2021-11-19',
    '2022-01-26', '2022-03-01', '2022-03-18', '2022-04-14', '2022-04-15', '2022-05-03', '2022-08-09', '2022-08-15', '2022-08-31', '2022-10-05', '2022-10-24', '2022-10-26', '2022-11-08',
    '2023-01-26', '2023-03-07', '2023-03-30', '2023-04-04', '2023-04-07', '2023-04-14', '2023-05-01', '2023-06-29', '2023-08-15', '2023-09-19', '2023-10-02', '2023-10-24', '2023-11-14', '2023-11-27', '2023-12-25',
    '2024-01-22', '2024-01-26', '2024-03-08', '2024-03-25',
    '2024-03-29', '2024-04-11', '2024-04-17', '2024-05-01',
    '2024-05-20', '2024-06-17', '2024-07-17', '2024-08-15',
    '2024-10-02', '2024-11-01', '2024-11-15', '2024-11-20',
    '2024-12-25',
    '2025-02-26', '2025-03-14', '2025-03-31', '2025-04-10',
    '2025-04-14', '2025-04-18', '2025-05-01', '2025-08-15',
    '2025-08-27', '2025-10-02', '2025-10-21', '2025-10-22',
    '2025-11-05', '2025-12-25',
    '2026-01-15', '2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31',
    '2026-04-03', '2026-04-14', '2026-05-01', '2026-05-28',
    '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20',
    '2026-11-10', '2026-11-24', '2026-12-25',
))


def regular_session_closed(day):
    return day.weekday() >= 5 or day in REGULAR_SESSION_HOLIDAYS
