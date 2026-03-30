/**
 * Projects Management Page
 *
 * Project = logical grouping (ConfigMap-based, no own namespace)
 * Environment = K8s namespace belonging to a project
 */

import { useState } from 'react';
import {
  Plus,
  Folder,
  Users,
  HardDrive,
  Server,
  Trash2,
  ChevronRight,
  ChevronDown,
  Search,
  X,
  Layers,
  Gauge,
  RefreshCw,
} from 'lucide-react';
import {
  useProjects,
  useCreateProject,
  useDeleteProject,
  useAddEnvironment,
  useRemoveEnvironment,
  useProjectAccess,
  useAddProjectAccess,
  useRemoveProjectAccess,
  useTeams,
} from '../hooks/useProjects';
import type { Project, Environment, Role, ProjectQuota } from '../types/project';
import { ROLE_LABELS, ROLE_DESCRIPTIONS } from '../types/project';
import { CustomSelect } from '../components/common/CustomSelect';
import { ConfirmDeleteModal } from '../components/common/ConfirmDeleteModal';

export default function Projects() {
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showAccessModal, setShowAccessModal] = useState<string | null>(null);
  const [showDeleteModal, setShowDeleteModal] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  const { data: projectsData, isLoading, refetch: refetchProjects } = useProjects();
  const deleteProject = useDeleteProject();

  const filteredProjects = projectsData?.items.filter(
    (p: Project) =>
      p.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      p.display_name.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const handleDelete = async (name: string) => {
    await deleteProject.mutateAsync(name);
    setShowDeleteModal(null);
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-surface-100">Projects</h1>
          <p className="text-sm text-surface-400 mt-1">
            Manage projects, environments, and team access
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => refetchProjects()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          <button
            onClick={() => setShowCreateModal(true)}
            className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 text-white rounded-lg transition-colors"
          >
            <Plus className="w-4 h-4" />
            Create Project
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-surface-400" />
        <input
          type="text"
          placeholder="Search projects..."
          value={searchQuery}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSearchQuery(e.target.value)}
          className="w-full pl-10 pr-4 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-400 focus:outline-none focus:border-cyan-500"
        />
      </div>

      {/* Projects List */}
      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <div className="w-8 h-8 border-2 border-cyan-500 border-t-transparent rounded-full animate-spin" />
        </div>
      ) : filteredProjects?.length === 0 ? (
        <div className="flex flex-col items-center justify-center h-64 text-surface-400">
          <Folder className="w-12 h-12 mb-4 opacity-50" />
          <p>No projects found</p>
          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              className="mt-2 text-cyan-400 hover:text-cyan-300"
            >
              Clear search
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-4">
          {filteredProjects?.map((project: Project) => (
            <ProjectRow
              key={project.name}
              project={project}
              onManageAccess={() => setShowAccessModal(project.name)}
              onDelete={() => setShowDeleteModal(project.name)}
            />
          ))}
        </div>
      )}

      {showCreateModal && (
        <CreateProjectModal onClose={() => setShowCreateModal(false)} />
      )}
      {showAccessModal && (
        <AccessModal
          projectName={showAccessModal}
          onClose={() => setShowAccessModal(null)}
        />
      )}
      <ConfirmDeleteModal
        isOpen={!!showDeleteModal}
        onClose={() => setShowDeleteModal(null)}
        onConfirm={() => showDeleteModal && handleDelete(showDeleteModal)}
        resourceName={showDeleteModal ?? ''}
        resourceType="Project"
        requireTyping
        isDeleting={deleteProject.isPending}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ProjectRow — expandable project with nested environments
// ---------------------------------------------------------------------------

function ProjectRow({
  project,
  onManageAccess,
  onDelete,
}: {
  project: Project;
  onManageAccess: () => void;
  onDelete: () => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [showAddEnv, setShowAddEnv] = useState(false);
  const [newEnvName, setNewEnvName] = useState('');
  const addEnv = useAddEnvironment(project.name);
  const removeEnv = useRemoveEnvironment(project.name);

  const handleAddEnv = async () => {
    const trimmed = newEnvName.trim().toLowerCase().replace(/[^a-z0-9-]/g, '-');
    if (!trimmed) return;
    await addEnv.mutateAsync({ environment: trimmed });
    setNewEnvName('');
    setShowAddEnv(false);
  };

  const [deleteEnvModal, setDeleteEnvModal] = useState<string | null>(null);

  const handleRemoveEnv = (envName: string) => {
    setDeleteEnvModal(envName);
  };

  const memberCount = project.teams.length + project.users.length;

  return (
    <div className="border border-surface-700 rounded-xl overflow-hidden">
      {/* Project header */}
      <div className="flex items-center justify-between p-4 bg-surface-800/80 hover:bg-surface-800 transition-colors">
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-3 flex-1 text-left"
        >
          {expanded ? (
            <ChevronDown className="w-4 h-4 text-surface-400 shrink-0" />
          ) : (
            <ChevronRight className="w-4 h-4 text-surface-400 shrink-0" />
          )}
          <div className="w-9 h-9 bg-cyan-600/20 rounded-lg flex items-center justify-center shrink-0">
            <Folder className="w-5 h-5 text-cyan-400" />
          </div>
          <div className="min-w-0">
            <h3 className="font-semibold text-surface-100">{project.display_name}</h3>
            <p className="text-xs text-surface-500">{project.name}</p>
          </div>
        </button>

        <div className="flex items-center gap-4 ml-4">
          {/* Stats */}
          <div className="hidden sm:flex items-center gap-4 text-xs text-surface-400">
            <span className="flex items-center gap-1">
              <Layers className="w-3.5 h-3.5" />
              {project.environments.length} env{project.environments.length !== 1 ? 's' : ''}
            </span>
            <span className="flex items-center gap-1">
              <Server className="w-3.5 h-3.5" />
              {project.total_vms} VMs
            </span>
            {project.total_storage && (
              <span className="flex items-center gap-1">
                <HardDrive className="w-3.5 h-3.5" />
                {project.total_storage}
              </span>
            )}
            {memberCount > 0 && (
              <span className="flex items-center gap-1">
                <Users className="w-3.5 h-3.5" />
                {memberCount}
              </span>
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-1">
            <button
              onClick={(e) => { e.stopPropagation(); onManageAccess(); }}
              className="p-2 text-surface-400 hover:text-cyan-400 hover:bg-surface-700 rounded-lg transition-colors"
              title="Manage access"
            >
              <Users className="w-4 h-4" />
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onDelete(); }}
              className="p-2 text-surface-400 hover:text-red-400 hover:bg-surface-700 rounded-lg transition-colors"
              title="Delete project"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Project Quota Usage */}
      {expanded && project.quota && (
        <div className="px-6 py-3 bg-surface-800/40 border-t border-surface-700/50">
          <div className="flex items-center gap-2 mb-2">
            <Gauge className="w-3.5 h-3.5 text-surface-400" />
            <span className="text-xs font-medium text-surface-400">Project Quota</span>
          </div>
          <div className="grid grid-cols-3 gap-4">
            {project.quota.cpu && (
              <QuotaBar
                label="CPU"
                used={sumEnvQuotas(project.environments, 'quota_cpu')}
                limit={project.quota.cpu}
              />
            )}
            {project.quota.memory && (
              <QuotaBar
                label="Memory"
                used={sumEnvQuotas(project.environments, 'quota_memory')}
                limit={project.quota.memory}
              />
            )}
            {project.quota.storage && (
              <QuotaBar
                label="Storage"
                used={sumEnvQuotas(project.environments, 'quota_storage')}
                limit={project.quota.storage}
              />
            )}
          </div>
        </div>
      )}

      {/* Environments */}
      {expanded && (
        <div className="bg-surface-900/40 border-t border-surface-700/50">
          {project.environments.length === 0 && !showAddEnv ? (
            <div className="flex items-center justify-center py-8 text-surface-500">
              <p className="text-sm">No environments yet.</p>
              <button
                onClick={() => setShowAddEnv(true)}
                className="ml-2 text-cyan-400 hover:text-cyan-300 text-sm"
              >
                Add one
              </button>
            </div>
          ) : (
            <div className="divide-y divide-surface-700/30">
              {project.environments.map((env: Environment) => (
                <div
                  key={env.name}
                  className="flex items-center justify-between px-6 py-3 hover:bg-surface-800/40 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <div className="w-2 h-2 rounded-full bg-emerald-500" />
                    <div>
                      <span className="text-sm font-medium text-surface-200">
                        {env.environment}
                      </span>
                      <span className="text-xs text-surface-500 ml-2">{env.name}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-4">
                    <div className="flex items-center gap-3 text-xs text-surface-400">
                      <span className="flex items-center gap-1">
                        <Server className="w-3 h-3" />
                        {env.vm_count}
                      </span>
                      {env.storage_used && (
                        <span className="flex items-center gap-1">
                          <HardDrive className="w-3 h-3" />
                          {env.storage_used}
                        </span>
                      )}
                      {(env.quota_cpu || env.quota_memory || env.quota_storage) && (
                        <span className="flex items-center gap-1 text-surface-500" title={`Quota: ${[env.quota_cpu && `CPU ${env.quota_cpu}`, env.quota_memory && `Mem ${env.quota_memory}`, env.quota_storage && `Storage ${env.quota_storage}`].filter(Boolean).join(', ')}`}>
                          <Gauge className="w-3 h-3" />
                          {[env.quota_cpu && `${env.quota_cpu} CPU`, env.quota_memory, env.quota_storage].filter(Boolean).join(' · ')}
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => handleRemoveEnv(env.environment)}
                      disabled={removeEnv.isPending}
                      className="p-1 text-surface-500 hover:text-red-400 rounded transition-colors"
                      title="Remove environment"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <ConfirmDeleteModal
            isOpen={!!deleteEnvModal}
            onClose={() => setDeleteEnvModal(null)}
            onConfirm={() => {
              if (deleteEnvModal) removeEnv.mutateAsync(deleteEnvModal).then(() => setDeleteEnvModal(null));
            }}
            resourceName={deleteEnvModal ?? ''}
            resourceType="Environment"
            isDeleting={removeEnv.isPending}
          />

          {/* Add environment */}
          <div className="px-4 py-3 border-t border-surface-700/30">
            {showAddEnv ? (
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={newEnvName}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                    setNewEnvName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))
                  }
                  placeholder="Environment name (e.g. dev, staging, prod)"
                  className="flex-1 px-3 py-1.5 bg-surface-800 border border-surface-600 rounded-lg text-sm text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500"
                  onKeyDown={(e: React.KeyboardEvent) => e.key === 'Enter' && handleAddEnv()}
                  autoFocus
                />
                <button
                  onClick={handleAddEnv}
                  disabled={!newEnvName.trim() || addEnv.isPending}
                  className="px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg text-sm transition-colors"
                >
                  {addEnv.isPending ? '...' : 'Add'}
                </button>
                <button
                  onClick={() => { setShowAddEnv(false); setNewEnvName(''); }}
                  className="p-1.5 text-surface-400 hover:text-surface-300"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            ) : (
              <button
                onClick={() => setShowAddEnv(true)}
                className="flex items-center gap-1.5 text-xs text-surface-500 hover:text-cyan-400 transition-colors"
              >
                <Plus className="w-3.5 h-3.5" />
                Add Environment
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// CreateProjectModal — wizard with text inputs + "+" for environments
// ---------------------------------------------------------------------------

function CreateProjectModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [description, setDescription] = useState('');
  const [environments, setEnvironments] = useState<string[]>(['dev']);
  const [newEnv, setNewEnv] = useState('');
  const [enableQuota, setEnableQuota] = useState(false);
  const [quotaCpu, setQuotaCpu] = useState('');
  const [quotaMemory, setQuotaMemory] = useState('');
  const [quotaStorage, setQuotaStorage] = useState('');

  const createProject = useCreateProject();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const envs = environments.filter((e) => e.trim().length > 0);
    let quota: ProjectQuota | undefined;
    if (enableQuota && (quotaCpu || quotaMemory || quotaStorage)) {
      quota = {
        cpu: quotaCpu || undefined,
        memory: quotaMemory || undefined,
        storage: quotaStorage || undefined,
      };
    }
    await createProject.mutateAsync({
      name,
      display_name: displayName,
      description: description || undefined,
      environments: envs.length > 0 ? envs : undefined,
      quota,
    });
    onClose();
  };

  const handleDisplayNameChange = (value: string) => {
    setDisplayName(value);
    if (!name || name === toKebabCase(displayName)) {
      setName(toKebabCase(value));
    }
  };

  const addEnvironment = () => {
    const trimmed = newEnv.trim().toLowerCase().replace(/[^a-z0-9-]/g, '-');
    if (trimmed && !environments.includes(trimmed)) {
      setEnvironments([...environments, trimmed]);
      setNewEnv('');
    }
  };

  const removeEnvironment = (idx: number) => {
    setEnvironments(environments.filter((_: string, i: number) => i !== idx));
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-lg mx-4 shadow-2xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-5 border-b border-surface-700 sticky top-0 bg-surface-800 z-10">
          <h2 className="text-lg font-semibold text-surface-100">Create Project</h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-300">
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 space-y-5">
          {/* Display Name */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Project Name *
            </label>
            <input
              type="text"
              value={displayName}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => handleDisplayNameChange(e.target.value)}
              placeholder="My Project"
              required
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500"
            />
          </div>

          {/* Slug */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Identifier *
            </label>
            <input
              type="text"
              value={name}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setName(e.target.value)}
              placeholder="my-project"
              required
              pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500 font-mono text-sm"
            />
            <p className="text-xs text-surface-500 mt-1">
              Used as a prefix for environment namespaces (e.g. {name || 'my-project'}-dev)
            </p>
          </div>

          {/* Description */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Description
            </label>
            <textarea
              value={description}
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setDescription(e.target.value)}
              placeholder="What is this project for?"
              rows={2}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500 resize-none"
            />
          </div>

          {/* Environments */}
          <div className="pt-4 border-t border-surface-700">
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Environments
            </label>
            <p className="text-xs text-surface-500 mb-3">
              Each environment creates a Kubernetes namespace: <span className="font-mono">{name || 'project'}-&lt;env&gt;</span>
            </p>

            {/* Existing environments */}
            <div className="space-y-2 mb-3">
              {environments.map((env: string, idx: number) => (
                <div key={idx} className="flex items-center gap-2">
                  <div className="flex-1 px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-sm text-surface-200 flex items-center justify-between">
                    <span>{env}</span>
                    <span className="text-xs text-surface-500 font-mono">
                      {name || 'project'}-{env}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => removeEnvironment(idx)}
                    className="p-1.5 text-surface-500 hover:text-red-400 transition-colors"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              ))}
            </div>

            {/* Add new environment */}
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={newEnv}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                  setNewEnv(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))
                }
                placeholder="Add environment (e.g. staging, prod)"
                className="flex-1 px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-sm text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500"
                onKeyDown={(e: React.KeyboardEvent) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    addEnvironment();
                  }
                }}
              />
              <button
                type="button"
                onClick={addEnvironment}
                disabled={!newEnv.trim()}
                className="p-1.5 bg-surface-700 hover:bg-surface-600 disabled:opacity-30 text-surface-300 rounded-lg transition-colors"
              >
                <Plus className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* Project Quota (optional) */}
          <div className="pt-4 border-t border-surface-700">
            <label className="flex items-center gap-2 cursor-pointer mb-3">
              <input
                type="checkbox"
                checked={enableQuota}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEnableQuota(e.target.checked)}
                className="rounded border-surface-600 text-cyan-600 focus:ring-cyan-500"
              />
              <span className="text-sm font-medium text-surface-300">Set project quota</span>
              <span className="text-xs text-surface-500">(optional soft limit)</span>
            </label>

            {enableQuota && (
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-xs text-surface-400 mb-1">CPU (cores)</label>
                  <input
                    type="text"
                    value={quotaCpu}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuotaCpu(e.target.value)}
                    placeholder="e.g. 16"
                    className="w-full px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-sm text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500"
                  />
                </div>
                <div>
                  <label className="block text-xs text-surface-400 mb-1">Memory</label>
                  <input
                    type="text"
                    value={quotaMemory}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuotaMemory(e.target.value)}
                    placeholder="e.g. 32Gi"
                    className="w-full px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-sm text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500"
                  />
                </div>
                <div>
                  <label className="block text-xs text-surface-400 mb-1">Storage</label>
                  <input
                    type="text"
                    value={quotaStorage}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuotaStorage(e.target.value)}
                    placeholder="e.g. 200Gi"
                    className="w-full px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-sm text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500"
                  />
                </div>
              </div>
            )}
          </div>

          {/* Submit */}
          <div className="flex justify-end gap-3 pt-4">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-surface-300 hover:text-surface-100 transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createProject.isPending || !name || !displayName}
              className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg transition-colors"
            >
              {createProject.isPending ? 'Creating...' : 'Create Project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AccessModal
// ---------------------------------------------------------------------------

function AccessModal({
  projectName,
  onClose,
}: {
  projectName: string;
  onClose: () => void;
}) {
  const [showAddForm, setShowAddForm] = useState(false);
  const [accessType, setAccessType] = useState<'team' | 'user'>('team');
  const [selectedName, setSelectedName] = useState('');
  const [selectedRole, setSelectedRole] = useState<Role>('editor');

  const { data: accessData, isLoading } = useProjectAccess(projectName);
  const { data: teamsData } = useTeams();
  const addAccess = useAddProjectAccess(projectName);
  const removeAccess = useRemoveProjectAccess(projectName);

  const handleAddAccess = async () => {
    if (!selectedName) return;
    await addAccess.mutateAsync({
      type: accessType,
      name: selectedName,
      role: selectedRole,
    });
    setShowAddForm(false);
    setSelectedName('');
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-2xl mx-4 shadow-2xl max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between p-5 border-b border-surface-700">
          <h2 className="text-lg font-semibold text-surface-100">
            Manage Access — {projectName}
          </h2>
          <button onClick={onClose} className="p-1 text-surface-400 hover:text-surface-300">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          <p className="text-xs text-surface-500 mb-4">
            Project-level access applies to all environments in this project.
          </p>

          {!showAddForm ? (
            <button
              onClick={() => setShowAddForm(true)}
              className="flex items-center gap-2 px-4 py-2 border border-dashed border-surface-600 hover:border-cyan-500 text-surface-400 hover:text-cyan-400 rounded-lg transition-colors w-full justify-center mb-4"
            >
              <Plus className="w-4 h-4" />
              Add Team or User
            </button>
          ) : (
            <div className="bg-surface-900/50 border border-surface-700 rounded-lg p-4 mb-4 space-y-4">
              <div className="flex gap-4">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="radio" value="team" checked={accessType === 'team'}
                    onChange={() => { setAccessType('team'); setSelectedName(''); }}
                    className="text-cyan-600" />
                  <span className="text-surface-300">Team</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="radio" value="user" checked={accessType === 'user'}
                    onChange={() => { setAccessType('user'); setSelectedName(''); }}
                    className="text-cyan-600" />
                  <span className="text-surface-300">User</span>
                </label>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm text-surface-400 mb-1">
                    {accessType === 'team' ? 'Select Team' : 'User Email'}
                  </label>
                  {accessType === 'team' ? (
                    <CustomSelect
                      value={selectedName}
                      onChange={setSelectedName}
                      placeholder="Select a team..."
                      options={[{ value: '', label: 'Select a team...' }, ...(teamsData?.items || []).map(t => ({ value: t.name, label: t.display_name }))]}
                    />
                  ) : (
                    <input type="email" value={selectedName}
                      onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSelectedName(e.target.value)}
                      placeholder="user@example.com"
                      className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-cyan-500" />
                  )}
                </div>
                <div>
                  <label className="block text-sm text-surface-400 mb-1">Role</label>
                  <CustomSelect
                    value={selectedRole}
                    onChange={(v) => setSelectedRole(v as Role)}
                    options={Object.entries(ROLE_LABELS).map(([v, l]) => ({ value: v, label: l }))}
                  />
                </div>
              </div>

              <p className="text-xs text-surface-500">{ROLE_DESCRIPTIONS[selectedRole]}</p>

              <div className="flex justify-end gap-2">
                <button onClick={() => { setShowAddForm(false); setSelectedName(''); }}
                  className="px-3 py-1.5 text-surface-400 hover:text-surface-300">Cancel</button>
                <button onClick={handleAddAccess} disabled={!selectedName || addAccess.isPending}
                  className="px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg text-sm">
                  {addAccess.isPending ? 'Adding...' : 'Add Access'}
                </button>
              </div>
            </div>
          )}

          {isLoading ? (
            <div className="flex justify-center py-8">
              <div className="w-6 h-6 border-2 border-cyan-500 border-t-transparent rounded-full animate-spin" />
            </div>
          ) : accessData?.items.length === 0 ? (
            <div className="text-center py-8 text-surface-400">
              <Users className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <p>No access configured yet</p>
            </div>
          ) : (
            <div className="space-y-2">
              {accessData?.items.map((entry) => (
                <div key={entry.id}
                  className="flex items-center justify-between p-3 bg-surface-900/50 border border-surface-700/50 rounded-lg">
                  <div className="flex items-center gap-3">
                    <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${
                      entry.type === 'team' ? 'bg-purple-600/20 text-purple-400' : 'bg-emerald-600/20 text-emerald-400'
                    }`}>
                      {entry.type === 'team' ? <Users className="w-4 h-4" /> : (
                        <span className="text-sm font-medium">{entry.name?.[0]?.toUpperCase() || '?'}</span>
                      )}
                    </div>
                    <div>
                      <div className="text-sm font-medium text-surface-200">{entry.name}</div>
                      <div className="text-xs text-surface-500">
                        {entry.type === 'team' ? 'Team' : 'User'}
                        {entry.scope === 'environment' && entry.environment && (
                          <span className="ml-1 text-cyan-500">· {entry.environment}</span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className={`px-2 py-1 text-xs rounded ${
                      entry.role === 'admin' ? 'bg-red-900/30 text-red-400'
                        : entry.role === 'editor' ? 'bg-amber-900/30 text-amber-400'
                        : 'bg-surface-700 text-surface-400'
                    }`}>{ROLE_LABELS[entry.role]}</span>
                    <button onClick={() => removeAccess.mutateAsync(entry.id)}
                      disabled={removeAccess.isPending}
                      className="p-1.5 text-surface-400 hover:text-red-400 hover:bg-red-900/20 rounded transition-colors"
                      title="Remove access">
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="flex justify-end p-4 border-t border-surface-700">
          <button onClick={onClose}
            className="px-4 py-2 bg-surface-700 hover:bg-surface-600 text-surface-100 rounded-lg transition-colors">
            Done
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Quota helpers
// ---------------------------------------------------------------------------

function parseQuotaValue(value: string): number {
  const units: Record<string, number> = {
    Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4,
    K: 1000, M: 1000 ** 2, G: 1000 ** 3, T: 1000 ** 4,
  };
  for (const [unit, mult] of Object.entries(units)) {
    if (value.endsWith(unit)) {
      return parseFloat(value.slice(0, -unit.length)) * mult;
    }
  }
  return parseFloat(value) || 0;
}

function sumEnvQuotas(
  environments: Environment[],
  field: 'quota_cpu' | 'quota_memory' | 'quota_storage'
): string {
  let total = 0;
  let hasSuffix = '';
  for (const env of environments) {
    const val = env[field];
    if (val) {
      // For CPU (plain numbers), just sum
      if (field === 'quota_cpu') {
        total += parseFloat(val) || 0;
      } else {
        total += parseQuotaValue(val);
        // Capture the suffix from the first value for display
        if (!hasSuffix) {
          const match = val.match(/[A-Za-z]+$/);
          hasSuffix = match ? match[0] : '';
        }
      }
    }
  }
  if (total === 0) return '0';
  if (field === 'quota_cpu') return String(total);
  // Format back with appropriate unit
  if (hasSuffix === 'Gi' || hasSuffix === 'GI') return `${(total / (1024 ** 3)).toFixed(0)}Gi`;
  if (hasSuffix === 'Mi') return `${(total / (1024 ** 2)).toFixed(0)}Mi`;
  if (hasSuffix === 'Ti') return `${(total / (1024 ** 4)).toFixed(0)}Ti`;
  return String(total);
}

function QuotaBar({ label, used, limit }: { label: string; used: string; limit: string }) {
  const usedNum = parseQuotaValue(used);
  const limitNum = parseQuotaValue(limit);
  const pct = limitNum > 0 ? Math.min((usedNum / limitNum) * 100, 100) : 0;
  const overLimit = usedNum > limitNum && limitNum > 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-surface-400">{label}</span>
        <span className={`text-xs font-mono ${overLimit ? 'text-red-400' : 'text-surface-300'}`}>
          {used} / {limit}
        </span>
      </div>
      <div className="h-1.5 bg-surface-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${
            overLimit ? 'bg-red-500' : pct > 80 ? 'bg-amber-500' : 'bg-cyan-500'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function toKebabCase(str: string): string {
  return str.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}
