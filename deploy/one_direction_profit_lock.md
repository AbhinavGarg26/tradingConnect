# One Direction Profit Lock V2.1 service

The service is paper-only. It uses completed database-backed NIFTY 15-minute
candles and live option quotes, selects the nearest listed unexpired NIFTY
weekly expiry, and chooses the nearest ATM CE or PE. It cannot place orders.

Validate broker access and the selected contracts without starting a session:

```sh
.venv/bin/python one_direction_profit_lock_service.py --user-id USER_ID --check
```

Install the paper timer only after the check succeeds:

```sh
sudo cp deploy/systemd/one-direction-profit-lock.service deploy/systemd/one-direction-profit-lock.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now one-direction-profit-lock.timer
```

`Persistent=false` prevents late catch-up. The service must accumulate paper
stop-fill evidence before a separately reviewed live execution path is added.
