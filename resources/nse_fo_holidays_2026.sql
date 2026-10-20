-- NSE F&O trading holidays for 2026 (NSE/FAOP/71777, 12 Dec 2025).
-- Keep trading_holidays updated from the exchange's annual circular.
INSERT INTO trading_holidays (holiday, holiday_date, created_at, updated_at)
SELECT dates.holiday, dates.holiday_date, NOW(), NOW()
FROM (VALUES
    ('Republic Day', DATE '2026-01-26'),
    ('Holi', DATE '2026-03-03'),
    ('Shri Ram Navami', DATE '2026-03-26'),
    ('Shri Mahavir Jayanti', DATE '2026-03-31'),
    ('Good Friday', DATE '2026-04-03'),
    ('Dr. Baba Saheb Ambedkar Jayanti', DATE '2026-04-14'),
    ('Maharashtra Day', DATE '2026-05-01'),
    ('Bakri Id', DATE '2026-05-28'),
    ('Muharram', DATE '2026-06-26'),
    ('Ganesh Chaturthi', DATE '2026-09-14'),
    ('Mahatma Gandhi Jayanti', DATE '2026-10-02'),
    ('Dussehra', DATE '2026-10-20'),
    ('Diwali-Balipratipada', DATE '2026-11-10'),
    ('Prakash Gurpurb Sri Guru Nanak Dev', DATE '2026-11-24'),
    ('Christmas', DATE '2026-12-25')
) AS dates(holiday, holiday_date)
WHERE NOT EXISTS (
    SELECT 1 FROM trading_holidays existing
    WHERE existing.holiday_date = dates.holiday_date
);
