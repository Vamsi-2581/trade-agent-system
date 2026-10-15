import os
import django
os.environ['DJANGO_SETTINGS_MODULE'] = 'core.settings.dev'
django.setup()

from apps.agent.models import TaskProgress, ScheduledOneTimeTask

print('=== TaskProgress (最近 10) ===')
for t in TaskProgress.objects.order_by('-created_at')[:10]:
    print(f'  task_id={str(t.task_id)[:8]} status={t.status} type={t.task_type} created={t.created_at}')
    if t.result:
        print(f'    result={str(t.result)[:300]!r}')

print()
print('=== ScheduledOneTimeTask (最近 10) ===')
for t in ScheduledOneTimeTask.objects.order_by('-created_at')[:10]:
    print(f'  id={t.id} name={t.task_name} agent={t.agent_name} status={t.status}')
    print(f'    message={str(t.message)[:200]!r}')
    if t.result:
        print(f'    result={str(t.result)[:200]!r}')

print()
print('=== BacktestResult box_breakout_* (最近 5) ===')
from apps.backtest.models import BacktestResult
for r in BacktestResult.objects.filter(strategy__name__icontains='box_breakout').order_by('-created_at')[:5]:
    print(f'  result_id={str(r.id)[:8]} symbol={r.symbol} tf={r.timeframe} ret={r.total_return_pct:.2f}% trades={r.total_trades} start={r.start_date} end={r.end_date}')
