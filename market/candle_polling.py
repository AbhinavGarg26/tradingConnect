"""Schedule read-only candle fetches at closes; bounded HTTP 429 retries only."""
from datetime import timedelta


class ClosedCandlePoller:
    def __init__(self,minutes=15,grace_seconds=5,retry_budget_seconds=90):
        self.minutes=minutes;self.grace_seconds=grace_seconds
        self.retry_budget=timedelta(seconds=retry_budget_seconds)
        self.last_success=None;self.retry_at=None;self.first_failure=None;self.failures=0

    def slot(self,now):
        opening=now.replace(hour=9,minute=15,second=0,microsecond=0)
        number=int((now-opening-timedelta(seconds=self.grace_seconds)).total_seconds()//(self.minutes*60))
        return (now.date(),number) if number>=1 else None

    def due(self,now):
        slot=self.slot(now)
        return slot is not None and slot!=self.last_success and (self.retry_at is None or now>=self.retry_at)

    def success(self,now):
        self.last_success=self.slot(now)
        self.retry_at=self.first_failure=None;self.failures=0

    def rate_limited(self,now):
        if self.first_failure is None:self.first_failure=now
        if now-self.first_failure>=self.retry_budget:
            raise RuntimeError('Candle reads remained unavailable; stop and reconcile rather than trade stale signals')
        delay=min(10*2**min(self.failures,2),40)
        self.failures+=1;self.retry_at=now+timedelta(seconds=delay)
        return delay


def is_rate_limit(exc):
    return getattr(exc,'code',None)==429
