import { useState } from 'react';
import { CalendarClock, Loader2, Check, X, Trash2, Play, Pause, PlayCircle } from 'lucide-react';
import { useSchedules, useCreateSchedule, useDeleteSchedule, useUpdateSchedule, useTriggerSchedule } from '@/hooks/useSchedules';
import { CustomSelect } from '@/components/common/CustomSelect';

export function ScheduleTab({ vm }: { vm: any }) {
  const { data: schedules, isLoading } = useSchedules(vm.namespace, vm.name);
  const createSchedule = useCreateSchedule();
  const deleteSchedule = useDeleteSchedule();
  const updateSchedule = useUpdateSchedule();
  const triggerSchedule = useTriggerSchedule();
  const [showForm, setShowForm] = useState(false);
  const [scheduleName, setScheduleName] = useState('');
  const [action, setAction] = useState('stop');
  const [cronExpr, setCronExpr] = useState('');
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const cronPresets = [
    { label: 'Daily 18:00', value: '0 18 * * *' },
    { label: 'Daily 08:00', value: '0 8 * * *' },
    { label: 'Sunday 03:00', value: '0 3 * * 0' },
    { label: 'Every 6h', value: '0 */6 * * *' },
  ];

  const handleCreate = () => {
    if (!scheduleName.trim() || !cronExpr.trim()) return;
    createSchedule.mutate(
      {
        namespace: vm.namespace,
        data: {
          name: scheduleName.trim(),
          action,
          schedule: cronExpr.trim(),
          vm_name: vm.name,
          vm_namespace: vm.namespace,
        },
      },
      { onSuccess: () => { setScheduleName(''); setCronExpr(''); setShowForm(false); } }
    );
  };

  const handleDelete = (name: string) => {
    deleteSchedule.mutate(
      { namespace: vm.namespace, name },
      { onSuccess: () => setDeleteConfirm(null) }
    );
  };

  const actionColors: Record<string, string> = {
    stop: 'text-amber-400 bg-amber-500/10',
    start: 'text-emerald-400 bg-emerald-500/10',
    restart: 'text-blue-400 bg-blue-500/10',
    delete: 'text-red-400 bg-red-500/10',
  };

  return (
    <div className="space-y-6">
      {/* Create schedule */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <div>
            <h3 className="font-display text-lg font-semibold">Scheduled Actions</h3>
            <p className="text-surface-400 text-sm mt-1">Automate VM actions on a schedule</p>
          </div>
          <button onClick={() => setShowForm(!showForm)} className="btn-secondary text-sm">
            <CalendarClock className="h-4 w-4" />
            {showForm ? 'Cancel' : 'Add Schedule'}
          </button>
        </div>

        {showForm && (
          <div className="px-4 py-3 border-b border-surface-700 bg-surface-900/30 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-surface-400 mb-1">Name</label>
                <input
                  type="text"
                  value={scheduleName}
                  onChange={(e) => setScheduleName(e.target.value)}
                  placeholder="e.g. auto-shutdown"
                  className="input w-full"
                />
              </div>
              <div>
                <label className="block text-xs text-surface-400 mb-1">Action</label>
                <CustomSelect
                  value={action}
                  onChange={setAction}
                  options={[
                    { value: 'stop', label: 'Stop VM' },
                    { value: 'start', label: 'Start VM' },
                    { value: 'restart', label: 'Restart VM' },
                    { value: 'delete', label: 'Delete VM' },
                  ]}
                />
              </div>
            </div>
            <div>
              <label className="block text-xs text-surface-400 mb-1">Cron Schedule</label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={cronExpr}
                  onChange={(e) => setCronExpr(e.target.value)}
                  placeholder="e.g. 0 18 * * * (daily at 18:00)"
                  className="input flex-1"
                />
                <div className="flex gap-1">
                  {cronPresets.map((p) => (
                    <button
                      key={p.value}
                      onClick={() => setCronExpr(p.value)}
                      className="btn-ghost text-xs px-2 py-1 whitespace-nowrap"
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <div className="flex justify-end">
              <button onClick={handleCreate} disabled={!scheduleName.trim() || !cronExpr.trim() || createSchedule.isPending} className="btn-primary">
                {createSchedule.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CalendarClock className="h-4 w-4" />}
                Create Schedule
              </button>
            </div>
            {createSchedule.error && (
              <p className="text-red-400 text-sm">{createSchedule.error.message}</p>
            )}
          </div>
        )}
      </div>

      {/* Schedules list */}
      <div className="card">
        <div className="card-header">
          <h3 className="font-display font-semibold">Active Schedules</h3>
        </div>
        {isLoading ? (
          <div className="p-8 flex items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-surface-400" />
          </div>
        ) : !schedules?.length ? (
          <div className="p-8 text-center text-surface-500">
            <CalendarClock className="h-8 w-8 mx-auto mb-2 opacity-50" />
            <p>No scheduled actions</p>
            <p className="text-xs mt-1">Create a schedule to automate VM operations</p>
          </div>
        ) : (
          <div className="divide-y divide-surface-700">
            {schedules.map((sched) => (
              <div key={sched.name} className="px-4 py-3 flex items-center justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-surface-200 truncate">{sched.name}</span>
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs ${actionColors[sched.action] || 'text-surface-400 bg-surface-700'}`}>
                      {sched.action}
                    </span>
                    {sched.suspended && (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-surface-700 text-surface-400">
                        <Pause className="h-3 w-3" />
                        paused
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-surface-500 mt-0.5 flex gap-3">
                    <span className="font-mono">{sched.schedule}</span>
                    {sched.last_schedule_time && (
                      <span>Last: {new Date(sched.last_schedule_time).toLocaleString()}</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  {/* Trigger now */}
                  <button
                    onClick={() => triggerSchedule.mutate({ namespace: vm.namespace, name: sched.name })}
                    className="btn-ghost p-1 text-surface-500 hover:text-primary-400"
                    title="Run now"
                    disabled={triggerSchedule.isPending}
                  >
                    <PlayCircle className="h-3.5 w-3.5" />
                  </button>
                  {/* Suspend/Resume */}
                  <button
                    onClick={() => updateSchedule.mutate({ namespace: vm.namespace, name: sched.name, data: { suspend: !sched.suspended } })}
                    className={`btn-ghost p-1 ${sched.suspended ? 'text-emerald-400 hover:text-emerald-300' : 'text-surface-500 hover:text-amber-400'}`}
                    title={sched.suspended ? 'Resume' : 'Pause'}
                  >
                    {sched.suspended ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
                  </button>
                  {/* Delete */}
                  {deleteConfirm === sched.name ? (
                    <div className="flex items-center gap-1">
                      <button onClick={() => handleDelete(sched.name)} className="btn-ghost p-1 text-red-400 hover:text-red-300" title="Confirm">
                        <Check className="h-3.5 w-3.5" />
                      </button>
                      <button onClick={() => setDeleteConfirm(null)} className="btn-ghost p-1" title="Cancel">
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ) : (
                    <button onClick={() => setDeleteConfirm(sched.name)} className="btn-ghost p-1 text-surface-500 hover:text-red-400" title="Delete">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
