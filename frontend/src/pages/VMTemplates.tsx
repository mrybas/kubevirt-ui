/**
 * VM Templates management page
 */

import { useState } from 'react';
import {
  LayoutTemplate,
  Plus,
  Trash2,
  Loader2,
  X,
  Cpu,
  MemoryStick,
  HardDrive,

  Folder,
  Pencil,
  Monitor,
  Terminal,
  RefreshCw,
  Search,
  Play,
  LayoutGrid,
  List,
} from 'lucide-react';
import { useTemplates, useGoldenImages, useCreateTemplate, useUpdateTemplate, useDeleteTemplate } from '../hooks/useTemplates';
import type { VMTemplate, VMTemplateCreate, GoldenImage } from '../types/template';
import { useNamespaces } from '../hooks/useNamespaces';
import { useAppStore } from '../store';
import { CustomSelect } from '../components/common/CustomSelect';
import { CreateVMWizard } from '../components/vm/CreateVMWizard';
import { ConfirmDeleteModal } from '../components/common/ConfirmDeleteModal';

const osIcons: Record<string, string> = {
  ubuntu: '🐧',
  centos: '🎩',
  debian: '🌀',
  fedora: '🎩',
  windows: '🪟',
  linux: '🐧',
};

type ViewMode = 'grid' | 'table';

export function VMTemplates() {
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editingTemplate, setEditingTemplate] = useState<VMTemplate | null>(null);
  const [createVMTemplate, setCreateVMTemplate] = useState<VMTemplate | null>(null);
  const [deleteModalTemplate, setDeleteModalTemplate] = useState<VMTemplate | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [filterProject, setFilterProject] = useState<string>('all');
  const [viewMode, setViewMode] = useState<ViewMode>('table');
  const { selectedNamespace } = useAppStore();
  
  const { data: templatesData, isLoading, refetch: refetchTemplates } = useTemplates();
  const { data: goldenImagesData } = useGoldenImages();
  const { data: namespacesData } = useNamespaces();
  const deleteTemplate = useDeleteTemplate();
  
  const templates = templatesData?.items || [];
  const goldenImages = goldenImagesData?.items || [];
  const projects = (namespacesData?.items || []).map(ns => ({ name: ns.name, display_name: (ns as any).display_name || ns.name }));
  
  // Get unique project namespaces from templates
  const templateProjects = [...new Set(templates.map(t => t.golden_image_namespace).filter(Boolean))];
  
  // Filter templates by search and project
  const filteredTemplates = templates.filter(t => {
    const matchesSearch = !searchQuery || 
      t.display_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      t.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (t.description || '').toLowerCase().includes(searchQuery.toLowerCase());
    const matchesProject = filterProject === 'all' || t.golden_image_namespace === filterProject;
    return matchesSearch && matchesProject;
  });
  
  const handleDelete = (template: VMTemplate) => {
    setDeleteModalTemplate(template);
  };

  const handleDeleteConfirm = async () => {
    if (!deleteModalTemplate) return;
    await deleteTemplate.mutateAsync(deleteModalTemplate.name);
    setDeleteModalTemplate(null);
  };
  
  const handleEdit = (template: VMTemplate) => {
    setEditingTemplate(template);
  };
  
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-surface-100">VM Templates</h1>
          <p className="text-surface-400 mt-1">
            Create and manage templates for quick VM provisioning
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => refetchTemplates()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          <button 
            className="btn-primary"
            onClick={() => setShowCreateModal(true)}
          >
            <Plus className="h-4 w-4" />
            Create Template
          </button>
        </div>
      </div>
      
      {/* Filters bar */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-surface-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search templates..."
            className="w-full pl-9 pr-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:ring-2 focus:ring-primary-500 text-sm"
          />
        </div>
        <CustomSelect
          value={filterProject}
          onChange={setFilterProject}
          placeholder="All Projects"
          options={[{ value: 'all', label: 'All Projects' }, ...templateProjects.map(ns => ({ value: String(ns), label: String(ns) }))]}
        />
        <div className="flex items-center border border-surface-700 rounded-lg overflow-hidden">
          <button
            onClick={() => setViewMode('grid')}
            className={`p-2 ${viewMode === 'grid' ? 'bg-primary-500/20 text-primary-400' : 'text-surface-400 hover:text-surface-200'}`}
            title="Grid view"
          >
            <LayoutGrid className="h-4 w-4" />
          </button>
          <button
            onClick={() => setViewMode('table')}
            className={`p-2 ${viewMode === 'table' ? 'bg-primary-500/20 text-primary-400' : 'text-surface-400 hover:text-surface-200'}`}
            title="Table view"
          >
            <List className="h-4 w-4" />
          </button>
        </div>
      </div>
      
      {/* Templates */}
      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <Loader2 className="w-8 h-8 animate-spin text-primary-400" />
        </div>
      ) : templates.length === 0 ? (
        <div className="card">
          <div className="card-body text-center py-16">
            <LayoutTemplate className="h-16 w-16 mx-auto text-surface-600 mb-4" />
            <h3 className="text-lg font-semibold text-surface-100 mb-2">
              No Templates yet
            </h3>
            <p className="text-surface-400 mb-6 max-w-md mx-auto">
              Create templates to quickly provision VMs with predefined configurations.
            </p>
            <button 
              className="btn-primary"
              onClick={() => setShowCreateModal(true)}
            >
              <Plus className="h-4 w-4" />
              Create Template
            </button>
          </div>
        </div>
      ) : filteredTemplates.length === 0 ? (
        <div className="card">
          <div className="card-body text-center py-12">
            <Search className="h-12 w-12 mx-auto text-surface-600 mb-3" />
            <p className="text-surface-400">No templates match your search.</p>
          </div>
        </div>
      ) : viewMode === 'grid' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredTemplates.map((template) => (
            <div 
              key={template.name} 
              className="card hover:border-surface-600 transition-colors group"
            >
              <div className="card-body">
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-3">
                    <div className="text-3xl">
                      {osIcons[template.icon || template.os_type] || '🖥️'}
                    </div>
                    <div>
                      <h4 className="font-medium text-surface-100">
                        {template.display_name}
                      </h4>
                      <p className="text-xs text-surface-500">{template.name}</p>
                    </div>
                  </div>
                  <span className="px-2 py-1 text-xs rounded bg-surface-700 text-surface-300">
                    {template.category}
                  </span>
                </div>
                
                {template.description && (
                  <p className="text-sm text-surface-400 mb-3">
                    {template.description}
                  </p>
                )}
                
                <div className="grid grid-cols-3 gap-2 text-sm">
                  <div className="bg-surface-800 rounded p-2 text-center">
                    <Cpu className="w-4 h-4 mx-auto text-primary-400 mb-1" />
                    <span className="text-surface-300">{template.compute.cpu_cores} vCPU</span>
                  </div>
                  <div className="bg-surface-800 rounded p-2 text-center">
                    <MemoryStick className="w-4 h-4 mx-auto text-emerald-400 mb-1" />
                    <span className="text-surface-300">{template.compute.memory}</span>
                  </div>
                  <div className="bg-surface-800 rounded p-2 text-center">
                    <HardDrive className="w-4 h-4 mx-auto text-amber-400 mb-1" />
                    <span className="text-surface-300">{template.disk.size}</span>
                  </div>
                </div>
                
                <div className="mt-3 pt-3 border-t border-surface-700 flex items-center justify-between">
                  <span className="text-xs text-surface-500 truncate mr-2">
                    {template.golden_image_namespace} / {template.golden_image_name}
                  </span>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setCreateVMTemplate(template)}
                      className="p-1.5 text-surface-400 hover:text-emerald-400 hover:bg-emerald-500/10 rounded-lg transition-colors"
                      title="Create VM from template"
                    >
                      <Play className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => handleEdit(template)}
                      className="p-1.5 text-surface-400 hover:text-primary-400 hover:bg-primary-500/10 rounded-lg transition-colors"
                      title="Edit"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => handleDelete(template)}
                      className="p-1.5 text-surface-400 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors"
                      title="Delete"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        /* Table view */
        <div className="card overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-surface-700 text-left">
                <th className="px-4 py-3 text-xs font-medium text-surface-500 uppercase">Template</th>
                <th className="px-4 py-3 text-xs font-medium text-surface-500 uppercase">Category</th>
                <th className="px-4 py-3 text-xs font-medium text-surface-500 uppercase">CPU</th>
                <th className="px-4 py-3 text-xs font-medium text-surface-500 uppercase">Memory</th>
                <th className="px-4 py-3 text-xs font-medium text-surface-500 uppercase">Disk</th>
                <th className="px-4 py-3 text-xs font-medium text-surface-500 uppercase">Project</th>
                <th className="px-4 py-3 text-xs font-medium text-surface-500 uppercase">Image</th>
                <th className="px-4 py-3 text-xs font-medium text-surface-500 uppercase text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {filteredTemplates.map((template) => (
                <tr key={template.name} className="hover:bg-surface-800/50 transition-colors">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="text-lg">{osIcons[template.icon || template.os_type] || '🖥️'}</span>
                      <div>
                        <div className="text-sm font-medium text-surface-100">{template.display_name}</div>
                        <div className="text-xs text-surface-500">{template.name}</div>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className="px-2 py-0.5 text-xs rounded bg-surface-700 text-surface-300">{template.category}</span>
                  </td>
                  <td className="px-4 py-3 text-sm text-surface-300">{template.compute.cpu_cores} vCPU</td>
                  <td className="px-4 py-3 text-sm text-surface-300">{template.compute.memory}</td>
                  <td className="px-4 py-3 text-sm text-surface-300">{template.disk.size}</td>
                  <td className="px-4 py-3 text-xs text-surface-400 font-mono">{template.golden_image_namespace}</td>
                  <td className="px-4 py-3 text-xs text-surface-400 font-mono">{template.golden_image_name}</td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => setCreateVMTemplate(template)}
                        className="p-1.5 text-surface-400 hover:text-emerald-400 hover:bg-emerald-500/10 rounded-lg transition-colors"
                        title="Create VM"
                      >
                        <Play className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => handleEdit(template)}
                        className="p-1.5 text-surface-400 hover:text-primary-400 hover:bg-primary-500/10 rounded-lg transition-colors"
                        title="Edit"
                      >
                        <Pencil className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(template)}
                        className="p-1.5 text-surface-400 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors"
                        title="Delete"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      
      {/* Create Modal */}
      {showCreateModal && (
        <TemplateModal
          goldenImages={goldenImages}
          projects={projects}
          defaultProject={selectedNamespace || ''}
          onClose={() => setShowCreateModal(false)}
        />
      )}
      
      {/* Edit Modal */}
      {editingTemplate && (
        <TemplateModal
          goldenImages={goldenImages}
          projects={projects}
          defaultProject={editingTemplate.golden_image_namespace || selectedNamespace || ''}
          editTemplate={editingTemplate}
          onClose={() => setEditingTemplate(null)}
        />
      )}
      
      {/* Create VM from Template */}
      {createVMTemplate && (
        <CreateVMWizard
          projects={projects.map(p => ({ name: p.name, display_name: p.display_name }))}
          defaultProject={createVMTemplate.golden_image_namespace}
          defaultTemplate={createVMTemplate}
          onClose={() => setCreateVMTemplate(null)}
          onSuccess={() => setCreateVMTemplate(null)}
        />
      )}

      <ConfirmDeleteModal
        isOpen={!!deleteModalTemplate}
        onClose={() => setDeleteModalTemplate(null)}
        onConfirm={handleDeleteConfirm}
        resourceName={deleteModalTemplate?.name ?? ''}
        resourceType="VM Template"
        isDeleting={deleteTemplate.isPending}
      />
    </div>
  );
}

interface TemplateModalProps {
  goldenImages: GoldenImage[];
  projects: { name: string; display_name?: string }[];
  defaultProject?: string;
  editTemplate?: VMTemplate;
  onClose: () => void;
}

function TemplateModal({ goldenImages, projects, defaultProject, editTemplate, onClose }: TemplateModalProps) {
  const isEditMode = !!editTemplate;
  
  const [name, setName] = useState(editTemplate?.name || '');
  const [displayName, setDisplayName] = useState(editTemplate?.display_name || '');
  const [description, setDescription] = useState(editTemplate?.description || '');
  const [category, setCategory] = useState(editTemplate?.category || 'linux');
  const [osType, setOsType] = useState(editTemplate?.os_type || 'linux');
  const [selectedProject, setSelectedProject] = useState(editTemplate?.golden_image_namespace || defaultProject || '');
  const [goldenImageName, setGoldenImageName] = useState(editTemplate?.golden_image_name || '');
  const [cpuCores, setCpuCores] = useState(editTemplate?.compute?.cpu_cores || 2);
  const [vcpu, setVcpu] = useState(editTemplate?.compute?.vcpu || editTemplate?.compute?.cpu_cores || 2);
  const [memory, setMemory] = useState(editTemplate?.compute?.memory || '4Gi');
  const [diskSize, setDiskSize] = useState(editTemplate?.disk?.size || '50Gi');
  
  // Console settings
  const [vncEnabled, setVncEnabled] = useState(editTemplate?.console?.vnc_enabled ?? true);
  const [serialConsoleEnabled, setSerialConsoleEnabled] = useState(editTemplate?.console?.serial_console_enabled ?? false);
  
  const createTemplate = useCreateTemplate();
  const updateTemplate = useUpdateTemplate();
  
  // Filter images by selected project
  const projectImages = goldenImages.filter(img => img.namespace === selectedProject);
  
  // Reset image selection when project changes (only in create mode)
  const handleProjectChange = (project: string) => {
    setSelectedProject(project);
    if (!isEditMode) {
      setGoldenImageName('');
    }
  };
  
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (!selectedProject || !goldenImageName) return;
    
    const data: VMTemplateCreate = {
      name: isEditMode ? editTemplate.name : name,
      display_name: displayName,
      description: description || undefined,
      category,
      os_type: osType,
      golden_image_name: goldenImageName,
      golden_image_namespace: selectedProject,
      compute: {
        cpu_cores: cpuCores,
        vcpu: vcpu,
        cpu_sockets: 1,
        cpu_threads: 1,
        memory,
      },
      disk: {
        size: diskSize,
      },
      network: {
        type: 'default',
      },
      console: {
        vnc_enabled: vncEnabled,
        serial_console_enabled: serialConsoleEnabled,
      },
    };
    
    try {
      if (isEditMode) {
        await updateTemplate.mutateAsync({ name: editTemplate.name, data });
      } else {
        await createTemplate.mutateAsync(data);
      }
      onClose();
    } catch (error) {
      console.error(`Failed to ${isEditMode ? 'update' : 'create'} template:`, error);
    }
  };
  
  const isValid = (isEditMode || name.length > 0) && displayName.length > 0 && selectedProject.length > 0 && goldenImageName.length > 0;
  const isPending = createTemplate.isPending || updateTemplate.isPending;
  const error = createTemplate.error || updateTemplate.error;
  
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-surface-800 border border-surface-700 rounded-xl shadow-2xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-700 sticky top-0 bg-surface-800">
          <h2 className="text-lg font-semibold text-surface-100">
            {isEditMode ? (
              <>
                <Pencil className="w-5 h-5 inline mr-2 text-primary-400" />
                Edit Template
              </>
            ) : (
              <>
                <LayoutTemplate className="w-5 h-5 inline mr-2 text-primary-400" />
                Create VM Template
              </>
            )}
          </h2>
          <button
            onClick={onClose}
            className="p-1.5 text-surface-400 hover:text-surface-200 hover:bg-surface-700 rounded-lg transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {error && (
            <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
              Failed to {isEditMode ? 'update' : 'create'} template. Please try again.
            </div>
          )}
          
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">
                Name *
              </label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
                placeholder="ubuntu-medium"
                className="input w-full"
                required
                disabled={isEditMode}
              />
              {isEditMode && (
                <p className="text-xs text-surface-500 mt-1">Name cannot be changed</p>
              )}
            </div>
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">
                Display Name *
              </label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Ubuntu Medium"
                className="input w-full"
                required
              />
            </div>
          </div>
          
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">
              Description
            </label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Medium-sized Ubuntu VM for development"
              className="input w-full"
            />
          </div>
          
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">
                Category
              </label>
              <CustomSelect
                value={category}
                onChange={setCategory}
                options={[
                  { value: 'linux', label: 'Linux' },
                  { value: 'windows', label: 'Windows' },
                  { value: 'development', label: 'Development' },
                  { value: 'production', label: 'Production' },
                ]}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">
                OS Type
              </label>
              <CustomSelect
                value={osType}
                onChange={setOsType}
                options={[
                  { value: 'linux', label: 'Linux' },
                  { value: 'windows', label: 'Windows' },
                ]}
              />
            </div>
          </div>
          
          {/* Project Selection */}
          <div className="p-4 bg-primary-500/5 border border-primary-500/20 rounded-lg">
            <label className="block text-sm font-medium text-surface-200 mb-1.5">
              <Folder className="w-4 h-4 inline mr-1.5 text-primary-400" />
              Project *
            </label>
            <CustomSelect
              value={selectedProject}
              onChange={handleProjectChange}
              placeholder="Select a project..."
              options={[{ value: '', label: 'Select a project...' }, ...projects.map(p => ({ value: p.name, label: p.display_name || p.name }))]}
            />
            <p className="mt-1.5 text-xs text-surface-500">
              Template and VMs created from it will be stored in this project. Only images from this project can be used.
            </p>
          </div>
          
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">
              Base Image *
            </label>
            {!selectedProject ? (
              <div className="p-3 bg-surface-700/50 border border-surface-600 rounded-lg text-surface-400 text-sm">
                Select a project first to see available images.
              </div>
            ) : projectImages.length === 0 ? (
              <div className="p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg text-amber-400 text-sm">
                No images in project "{selectedProject}". Import images in the Storage section first.
              </div>
            ) : (
              <CustomSelect
                value={goldenImageName}
                onChange={setGoldenImageName}
                placeholder="Select an image..."
                options={[{ value: '', label: 'Select an image...' }, ...projectImages.map(img => ({ value: img.name, label: `${img.display_name || img.name} (${img.size || 'N/A'})` }))]}
              />
            )}
          </div>
          
          <div className="border-t border-surface-700 pt-4">
            <h3 className="text-sm font-medium text-surface-200 mb-3">
              Default Resources
            </h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  CPU (real cores)
                </label>
                <input
                  type="number"
                  value={cpuCores}
                  onChange={(e) => setCpuCores(parseInt(e.target.value) || 1)}
                  min={1}
                  max={64}
                  className="input w-full"
                />
                <p className="text-xs text-surface-500 mt-1">Scheduler limits / overcommit base</p>
              </div>
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  vCPU (VM visible)
                </label>
                <input
                  type="number"
                  value={vcpu}
                  onChange={(e) => setVcpu(parseInt(e.target.value) || 1)}
                  min={1}
                  max={128}
                  className="input w-full"
                />
                <p className="text-xs text-surface-500 mt-1">How many CPUs the VM sees</p>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-4 mt-4">
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  Memory
                </label>
                <CustomSelect
                  value={memory}
                  onChange={setMemory}
                  options={[
                    { value: '1Gi', label: '1 GB' },
                    { value: '2Gi', label: '2 GB' },
                    { value: '4Gi', label: '4 GB' },
                    { value: '8Gi', label: '8 GB' },
                    { value: '16Gi', label: '16 GB' },
                    { value: '32Gi', label: '32 GB' },
                  ]}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  Disk Size
                </label>
                <CustomSelect
                  value={diskSize}
                  onChange={setDiskSize}
                  options={[
                    { value: '20Gi', label: '20 GB' },
                    { value: '50Gi', label: '50 GB' },
                    { value: '100Gi', label: '100 GB' },
                    { value: '200Gi', label: '200 GB' },
                  ]}
                />
              </div>
            </div>
          </div>
          
          {/* Console Settings */}
          <div className="border-t border-surface-700 pt-4">
            <h3 className="text-sm font-medium text-surface-200 mb-3">
              Console Settings
            </h3>
            <div className="space-y-3">
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={vncEnabled}
                  onChange={(e) => setVncEnabled(e.target.checked)}
                  className="w-4 h-4 rounded border-surface-600 bg-surface-700 text-primary-500 focus:ring-primary-500 focus:ring-offset-0"
                />
                <div className="flex items-center gap-2">
                  <Monitor className="w-4 h-4 text-primary-400" />
                  <span className="text-sm text-surface-300">Enable VNC Console</span>
                </div>
                <span className="text-xs text-surface-500">(graphical console)</span>
              </label>
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={serialConsoleEnabled}
                  onChange={(e) => setSerialConsoleEnabled(e.target.checked)}
                  className="w-4 h-4 rounded border-surface-600 bg-surface-700 text-primary-500 focus:ring-primary-500 focus:ring-offset-0"
                />
                <div className="flex items-center gap-2">
                  <Terminal className="w-4 h-4 text-emerald-400" />
                  <span className="text-sm text-surface-300">Enable Serial Console</span>
                </div>
                <span className="text-xs text-surface-500">(text-only, useful for debugging)</span>
              </label>
            </div>
          </div>
          
          <div className="flex justify-end gap-3 pt-4 border-t border-surface-700">
            <button type="button" onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button 
              type="submit" 
              disabled={!isValid || isPending} 
              className="btn-primary"
            >
              {isPending ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  {isEditMode ? 'Saving...' : 'Creating...'}
                </>
              ) : (
                <>
                  {isEditMode ? <Pencil className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
                  {isEditMode ? 'Save Changes' : 'Create Template'}
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
