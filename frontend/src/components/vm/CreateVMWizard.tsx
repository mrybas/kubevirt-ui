/**
 * Create VM Wizard - Step-by-step VM creation from template
 */

import { useState, useEffect, useRef } from 'react';
import { X, Server, HardDrive, Cpu, MemoryStick, Check, Loader2, FolderOpen, Network, Globe, Plus, Trash2, Gauge, ChevronRight, Folder } from 'lucide-react';
import { useTemplates, useGoldenImages, useCreateVMFromTemplate } from '@/hooks/useTemplates';
import { useSubnets } from '@/hooks/useNetwork';
import type { VMTemplate, VMFromTemplateRequest } from '@/types/template';
import { CustomSelect } from '@/components/common/CustomSelect';
import { WizardStepIndicator } from '@/components/common/WizardStepIndicator';
import { SSHKeyPicker } from '@/components/vm/SSHKeyPicker';
import { useFoldersFlat } from '@/hooks/useFolders';
import type { Folder as FolderType } from '@/types/folder';

interface Project {
  name: string;
  display_name?: string;
}

interface CreateVMWizardProps {
  projects: Project[];
  defaultProject?: string;
  defaultTemplate?: VMTemplate;
  defaultFolderName?: string;  // pre-select folder context
  onClose: () => void;
  onSuccess: () => void;
}

type WizardStep = 'template' | 'customize' | 'network' | 'cloudInit' | 'review';

// Helper: parse "4Gi" → { value: 4, unit: "Gi" }
function parseSize(s: string): { value: number; unit: string } {
  const match = s.match(/^(\d+(?:\.\d+)?)\s*(Mi|Gi|Ti)$/i);
  if (match) return { value: parseFloat(match[1]!), unit: match[2]! };
  return { value: parseFloat(s) || 0, unit: 'Gi' };
}

function SizeInput({ value, onChange, units, defaultUnit }: {
  value: string;
  onChange: (v: string) => void;
  units: string[];
  defaultUnit: string;
}) {
  const parsed = parseSize(value || `0${defaultUnit}`);
  const [num, setNum] = useState(String(parsed.value || ''));
  const [unit, setUnit] = useState(parsed.unit || defaultUnit);

  // Sync internal state when value prop changes (e.g. template selection)
  useEffect(() => {
    const p = parseSize(value || `0${defaultUnit}`);
    setNum(p.value ? String(p.value) : '');
    setUnit(p.unit || defaultUnit);
  }, [value, defaultUnit]);

  const update = (n: string, u: string) => {
    setNum(n);
    setUnit(u);
    const v = parseFloat(n);
    if (v > 0) onChange(`${v}${u}`);
  };

  return (
    <div className="flex rounded-md border border-surface-700 overflow-hidden focus-within:ring-2 focus-within:ring-primary-500 focus-within:border-primary-500">
      <input
        type="number"
        value={num}
        onChange={(e) => update(e.target.value, unit)}
        min={1}
        className="flex-1 min-w-0 px-3 py-2 bg-surface-800 text-surface-100 focus:outline-none [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
        placeholder="0"
      />
      <div className="flex border-l border-surface-700">
        {units.map((u) => (
          <button
            key={u}
            type="button"
            onClick={() => update(num, u)}
            className={`px-2.5 py-2 text-xs font-medium transition-colors ${
              unit === u
                ? 'bg-primary-500/20 text-primary-300'
                : 'bg-surface-800 text-surface-500 hover:text-surface-200'
            }`}
          >
            {u === 'Mi' ? 'MB' : u === 'Gi' ? 'GB' : u === 'Ti' ? 'TB' : u}
          </button>
        ))}
      </div>
    </div>
  );
}

const osIcons: Record<string, string> = {
  ubuntu: '🐧',
  centos: '🎩',
  debian: '🌀',
  fedora: '🎩',
  windows: '🪟',
  linux: '🐧',
};

// ---------------------------------------------------------------------------
// FolderQuotaWarning — shows quota when a folder with limits is selected
// ---------------------------------------------------------------------------

function FolderQuotaWarning({ folder }: { folder: FolderType }) {
  if (!folder.quota) return null;
  const { cpu, memory, storage } = folder.quota;
  const parts = [
    cpu && `CPU: ${cpu}`,
    memory && `Mem: ${memory}`,
    storage && `Storage: ${storage}`,
  ].filter(Boolean);
  if (parts.length === 0) return null;

  return (
    <div className="flex items-start gap-2 p-2.5 bg-amber-500/10 border border-amber-500/30 rounded-lg text-xs text-amber-400">
      <Gauge className="h-3.5 w-3.5 shrink-0 mt-0.5" />
      <span>
        Folder quota: <span className="font-medium">{parts.join(' · ')}</span>. Ensure the VM fits within these limits.
      </span>
    </div>
  );
}

export function CreateVMWizard({ projects, defaultProject, defaultTemplate, defaultFolderName, onClose, onSuccess }: CreateVMWizardProps) {
  const [step, setStep] = useState<WizardStep>(defaultTemplate ? 'customize' : 'template');
  const [selectedTemplate, setSelectedTemplate] = useState<VMTemplate | null>(defaultTemplate || null);
  const [selectedProject, setSelectedProject] = useState(defaultTemplate?.golden_image_namespace || defaultProject || '');
  const [selectedFolderName, setSelectedFolderName] = useState(defaultFolderName ?? '');

  const { data: foldersData } = useFoldersFlat();
  const allFolders = foldersData?.items ?? [];
  const selectedFolder = allFolders.find((f) => f.name === selectedFolderName) ?? null;
  
  // Form state — pre-populate from defaultTemplate if provided
  const [vmName, setVmName] = useState('');
  const [vmCount, setVmCount] = useState(1);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const [batchProgress, setBatchProgress] = useState<{ current: number; total: number; failed: string[] } | null>(null);
  const [cpuCores, setCpuCores] = useState<number | undefined>(defaultTemplate?.compute.cpu_cores);
  const [memory, setMemory] = useState<string | undefined>(defaultTemplate?.compute.memory);
  const [diskSize, setDiskSize] = useState<string | undefined>(defaultTemplate?.disk.size);
  const [sshKey, setSshKey] = useState('');
  const [password, setPassword] = useState('');
  const [startVM, setStartVM] = useState(true);
  // Network — multi-NIC support
  interface NICConfig { subnet: string; staticIP: string; }
  const [nics, setNics] = useState<NICConfig[]>([]);
  
  const { data: templatesData, isLoading: templatesLoading } = useTemplates();
  const { data: subnets } = useSubnets();
  const { data: goldenImagesData } = useGoldenImages();
  const createVM = useCreateVMFromTemplate();
  
  const templates = templatesData?.items || [];
  const goldenImages = goldenImagesData?.items || [];
  
  // Filter templates by selected project (templates can only use images from the same namespace)
  // And check if the golden image exists in that project
  const projectImages = goldenImages.filter(img => img.namespace === selectedProject);
  const projectImageNames = new Set(projectImages.map(img => img.name));
  
  // Templates from the selected project that have valid images
  const availableTemplates = templates.filter(t => 
    t.golden_image_namespace === selectedProject && 
    projectImageNames.has(t.golden_image_name)
  );
  
  // Generate VM names based on pattern and count
  const generateVMNames = (): string[] => {
    const count = vmCount || 1;
    if (count === 1 && !vmName.includes('{n}')) return [vmName];
    const padLen = String(count).length;
    return Array.from({ length: count }, (_, i) => {
      const num = String(i + 1).padStart(padLen, '0');
      return vmName.includes('{n}')
        ? vmName.replace(/\{n\}/g, num)
        : `${vmName}-${num}`;
    });
  };
  
  // Insert {n} counter placeholder at cursor position in name input
  const insertCounter = () => {
    const input = nameInputRef.current;
    const start = input?.selectionStart ?? vmName.length;
    const end = input?.selectionEnd ?? vmName.length;
    const newName = vmName.slice(0, start) + '{n}' + vmName.slice(end);
    setVmName(newName);
    requestAnimationFrame(() => {
      input?.focus();
      const pos = start + 3;
      input?.setSelectionRange(pos, pos);
    });
  };
  
  const handleSubmit = async () => {
    if (!selectedTemplate || !selectedProject) return;
    
    const names = generateVMNames();
    const failed: string[] = [];
    setBatchProgress({ current: 0, total: names.length, failed: [] });
    
    for (let i = 0; i < names.length; i++) {
      const request: VMFromTemplateRequest = {
        name: names[i]!,
        template_name: selectedTemplate.name,
        start: startVM,
      };
      
      if (cpuCores) request.cpu_cores = cpuCores;
      if (memory) request.memory = memory;
      if (diskSize) request.disk_size = diskSize;
      if (sshKey) request.ssh_key = sshKey;
      if (password) request.password = password;
      if (nics.length > 0) {
        request.networks = nics.map(n => ({
          subnet: n.subnet,
          static_ip: n.staticIP || undefined,
        }));
      }
      
      try {
        await createVM.mutateAsync({ namespace: selectedProject, data: request });
      } catch (error) {
        failed.push(names[i]!);
      }
      setBatchProgress({ current: i + 1, total: names.length, failed: [...failed] });
    }
    
    if (failed.length === 0) {
      onSuccess();
    }
  };
  
  const allSteps: WizardStep[] = ['template', 'customize', 'network', 'cloudInit', 'review'];
  const stepLabels = ['Template', 'Customize', 'Network', 'Cloud Init', 'Review'];
  
  const renderTemplateStep = () => (
    <div className="space-y-4">
      {/* Folder + Project Selection */}
      <div className="p-4 bg-primary-500/5 border border-primary-500/20 rounded-lg space-y-3">
        {/* Folder picker */}
        <div>
          <label className="block text-sm font-medium text-surface-200 mb-1.5">
            <Folder className="w-4 h-4 inline mr-1.5 text-primary-400" />
            Folder (optional)
          </label>
          <CustomSelect
            value={selectedFolderName}
            onChange={(v) => {
              setSelectedFolderName(v);
              // If folder has exactly one environment, auto-select it
              const folder = allFolders.find((f) => f.name === v);
              if (folder && folder.environments.length === 1) {
                setSelectedProject(folder.environments[0]!.name);
                setSelectedTemplate(null);
              }
            }}
            placeholder="Select a folder..."
            options={[
              { value: '', label: '— No folder —' },
              ...allFolders.map((f) => ({
                value: f.name,
                label: f.path.length > 0 ? `${f.path.join(' › ')} › ${f.display_name}` : f.display_name,
              })),
            ]}
          />
        </div>

        {/* Folder breadcrumb */}
        {selectedFolder && (
          <div className="flex items-center gap-1 text-xs text-surface-400 flex-wrap">
            <Folder className="h-3.5 w-3.5 text-surface-500" />
            {selectedFolder.path.map((p) => (
              <span key={p} className="flex items-center gap-1">
                <span>{allFolders.find((f) => f.name === p)?.display_name ?? p}</span>
                <ChevronRight className="h-3 w-3 text-surface-600" />
              </span>
            ))}
            <span className="text-surface-200 font-medium">{selectedFolder.display_name}</span>
          </div>
        )}

        {/* Quota warning */}
        {selectedFolder?.quota && (
          <FolderQuotaWarning folder={selectedFolder} />
        )}

        {/* Project (namespace) Selection */}
        <div>
          <label className="block text-sm font-medium text-surface-200 mb-1.5">
            <FolderOpen className="w-4 h-4 inline mr-1.5 text-primary-400" />
            Environment (namespace) *
          </label>
          <CustomSelect
            value={selectedProject}
            onChange={(v) => {
              setSelectedProject(v);
              setSelectedTemplate(null);
            }}
            placeholder="Select an environment..."
            options={[
              { value: '', label: 'Select an environment...' },
              ...(selectedFolder
                ? selectedFolder.environments.map((e) => ({
                    value: e.name,
                    label: `${e.environment} (${e.name})`,
                  }))
                : projects.map((p) => ({ value: p.name, label: p.display_name || p.name }))),
            ]}
          />
          <p className="mt-1.5 text-xs text-surface-500">
            VM will be created in this namespace. Only templates from this namespace can be used.
          </p>
        </div>
      </div>

      <h3 className="text-lg font-semibold text-surface-100 pt-2">Select a Template</h3>
      
      {!selectedProject ? (
        <div className="text-center py-12 text-surface-400 bg-surface-800/50 rounded-lg">
          <FolderOpen className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p>Select a project first to see available templates.</p>
        </div>
      ) : templatesLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="w-8 h-8 animate-spin text-primary-400" />
        </div>
      ) : availableTemplates.length === 0 ? (
        <div className="text-center py-12 text-surface-400 bg-surface-800/50 rounded-lg">
          <Server className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p>No templates in project "{selectedProject}".</p>
          <p className="text-sm mt-2">Create a template first or import images in this project.</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4">
          {availableTemplates.map((template) => (
            <button
              key={template.name}
              onClick={() => {
                setSelectedTemplate(template);
                setCpuCores(template.compute.cpu_cores);
                setMemory(template.compute.memory);
                setDiskSize(template.disk.size);
              }}
              className={`p-4 rounded-lg border text-left transition-all ${
                selectedTemplate?.name === template.name
                  ? 'border-primary-500 bg-primary-500/10'
                  : 'border-surface-700 hover:border-surface-600 bg-surface-800'
              }`}
            >
              <div className="flex items-start gap-3">
                <div className="text-3xl">
                  {osIcons[template.icon || template.os_type] || '🖥️'}
                </div>
                <div className="flex-1 min-w-0">
                  <h4 className="font-medium text-surface-100 truncate">
                    {template.display_name}
                  </h4>
                  <p className="text-sm text-surface-400 truncate">
                    {template.description || template.name}
                  </p>
                  <div className="flex gap-3 mt-2 text-xs text-surface-500">
                    <span className="flex items-center gap-1">
                      <Cpu className="w-3 h-3" />
                      {template.compute.cpu_cores} vCPU
                    </span>
                    <span className="flex items-center gap-1">
                      <MemoryStick className="w-3 h-3" />
                      {template.compute.memory}
                    </span>
                    <span className="flex items-center gap-1">
                      <HardDrive className="w-3 h-3" />
                      {template.disk.size}
                    </span>
                  </div>
                </div>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
  
  const renderCustomizeStep = () => {
    const names = generateVMNames();
    
    return (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-surface-100">Customize VM</h3>
        
        <div>
          <label className="block text-sm font-medium text-surface-300 mb-1">
            VM Name *
          </label>
          <div className="flex gap-2">
            <input
              ref={nameInputRef}
              type="text"
              value={vmName}
              onChange={(e) => setVmName(e.target.value.toLowerCase().replace(/[^a-z0-9\-{}]/g, ''))}
              placeholder={vmCount > 1 ? 'my-vm-{n}' : 'my-vm'}
              className="flex-1 px-3 py-2 bg-surface-800 border border-surface-700 rounded-md text-surface-100 placeholder-surface-500 focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono"
            />
            <button
              type="button"
              onClick={insertCounter}
              className="px-3 py-2 bg-primary-500/10 border border-primary-500/30 text-primary-300 rounded-md text-sm font-mono hover:bg-primary-500/20 transition-colors whitespace-nowrap"
              title="Insert counter placeholder at cursor position"
            >
              {'{n}'}
            </button>
          </div>
          <p className="text-xs text-surface-500 mt-1">
            Lowercase letters, numbers, and hyphens. Use <span className="font-mono text-primary-400">{'{n}'}</span> for auto-numbering.
          </p>
        </div>
        
        <div>
          <label className="block text-sm font-medium text-surface-300 mb-1">
            Number of VMs
          </label>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setVmCount(Math.max(1, vmCount - 1))}
              className="w-9 h-9 flex items-center justify-center bg-surface-800 border border-surface-700 rounded-md text-surface-300 hover:bg-surface-700 transition-colors text-lg"
            >
              −
            </button>
            <input
              type="number"
              value={vmCount}
              onChange={(e) => setVmCount(Math.max(1, Math.min(50, parseInt(e.target.value) || 1)))}
              min={1}
              max={50}
              className="w-16 px-2 py-2 bg-surface-800 border border-surface-700 rounded-md text-surface-100 text-center focus:outline-none focus:ring-2 focus:ring-primary-500 [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
            />
            <button
              type="button"
              onClick={() => setVmCount(Math.min(50, vmCount + 1))}
              className="w-9 h-9 flex items-center justify-center bg-surface-800 border border-surface-700 rounded-md text-surface-300 hover:bg-surface-700 transition-colors text-lg"
            >
              +
            </button>
            {vmCount > 1 && (
              <span className="text-xs text-surface-400 ml-1">
                {vmCount} VMs will be created
              </span>
            )}
          </div>
        </div>
        
        {vmCount > 1 && vmName && (
          <div className="bg-surface-800/50 border border-surface-700 rounded-lg p-3">
            <p className="text-xs font-medium text-surface-400 mb-1.5">Preview ({vmCount} VMs)</p>
            <div className="flex flex-wrap gap-1.5">
              {names.slice(0, 5).map((n) => (
                <span key={n} className="px-2 py-0.5 bg-surface-700 text-surface-200 rounded text-xs font-mono">{n}</span>
              ))}
              {names.length > 5 && (
                <span className="px-2 py-0.5 text-surface-500 text-xs">...and {names.length - 5} more</span>
              )}
            </div>
            {!vmName.includes('{n}') && (
              <p className="text-xs text-amber-400 mt-2">
                Tip: Add <button type="button" onClick={insertCounter} className="font-mono underline">{'{n}'}</button> to the name for sequential numbering
              </p>
            )}
          </div>
        )}
        
        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              CPU Cores
            </label>
            <input
              type="number"
              value={cpuCores || ''}
              onChange={(e) => setCpuCores(parseInt(e.target.value) || undefined)}
              min={1}
              max={64}
              className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-md text-surface-100 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
          
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Memory
            </label>
            <SizeInput value={memory || ''} onChange={(v) => setMemory(v)} units={['Mi', 'Gi']} defaultUnit="Gi" />
          </div>
          
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Disk Size
            </label>
            <SizeInput value={diskSize || ''} onChange={(v) => setDiskSize(v)} units={['Gi', 'Ti']} defaultUnit="Gi" />
          </div>
        </div>
      </div>
    );
  };
  
  const renderNetworkStep = () => {
    // Filter subnets that have a VLAN (external networks) and match the VM's target namespace
    const externalSubnets = subnets?.filter((s: any) => s.vlan && (!s.namespace || s.namespace === selectedProject)) || [];

    const addNic = (subnetName: string) => {
      if (!nics.find(n => n.subnet === subnetName)) {
        setNics([...nics, { subnet: subnetName, staticIP: '' }]);
      }
    };
    const removeNic = (idx: number) => {
      setNics(nics.filter((_, i) => i !== idx));
    };
    
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold text-surface-100">Network Configuration</h3>
          {nics.length > 0 && (
            <span className="text-xs px-2 py-1 rounded bg-emerald-500/10 text-emerald-400">
              {nics.length} NIC{nics.length > 1 ? 's' : ''} selected
            </span>
          )}
        </div>

        <p className="text-sm text-surface-400">
          {nics.length === 0
            ? 'No external NICs selected — VM will use the default pod network (cluster-internal IP).'
            : 'Click a subnet to remove it. The first NIC becomes the default network.'}
        </p>

        {/* Selected NICs */}
        {nics.length > 0 && (
          <div className="space-y-3">
            {nics.map((nic, idx) => {
              const info = externalSubnets.find((s: any) => s.name === nic.subnet);
              return (
                <div key={nic.subnet} className="p-4 rounded-lg border border-emerald-500/40 bg-emerald-500/5">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <Globe className="w-4 h-4 text-emerald-400" />
                      <span className="text-sm font-medium text-surface-100">
                        NIC {idx + 1}{idx === 0 ? ' (default)' : ''}
                      </span>
                      <span className="text-xs font-mono text-surface-400">{nic.subnet}</span>
                    </div>
                    <button
                      onClick={() => removeNic(idx)}
                      className="p-1 hover:bg-surface-700 rounded text-surface-500 hover:text-red-400 transition-colors"
                      title="Remove NIC"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                  {info && (
                    <div className="flex items-center gap-4 text-xs text-surface-400">
                      <span>CIDR: <span className="font-mono text-surface-300">{info.cidr_block}</span></span>
                      <span>GW: <span className="font-mono text-surface-300">{info.gateway}</span></span>
                      <span className="text-emerald-400">{info.statistics?.available || 0} IPs free</span>
                      <span className="text-surface-500">DHCP</span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Available subnets as tiles */}
        <div>
          <h4 className="text-sm font-medium text-surface-300 mb-2">
            {nics.length === 0 ? 'Available External Networks' : 'Add Another NIC'}
          </h4>
          {externalSubnets.length === 0 ? (
            <div className="text-center py-8 text-surface-500 bg-surface-800/50 rounded-lg">
              <Network className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <p className="text-sm">No external networks configured for this project.</p>
              <p className="text-xs mt-1">VM will use the default pod network.</p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-2">
              {externalSubnets.map((subnet: any) => {
                const isAdded = !!nics.find(n => n.subnet === subnet.name);
                return (
                  <button
                    key={subnet.name}
                    onClick={() => isAdded ? removeNic(nics.findIndex(n => n.subnet === subnet.name)) : addNic(subnet.name)}
                    className={`p-3 rounded-lg border text-left transition-all ${
                      isAdded
                        ? 'border-emerald-500/40 bg-emerald-500/5 opacity-50'
                        : 'border-surface-700 hover:border-emerald-500/40 hover:bg-emerald-500/5 bg-surface-800'
                    }`}
                  >
                    <div className="flex items-center gap-3">
                      <div className={`p-1.5 rounded-lg ${isAdded ? 'bg-emerald-500/20' : 'bg-surface-700'}`}>
                        <Globe className={`w-4 h-4 ${isAdded ? 'text-emerald-400' : 'text-surface-400'}`} />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-surface-100">{subnet.name}</span>
                          <span className="text-xs font-mono text-surface-500">{subnet.cidr_block}</span>
                          {isAdded && <Check className="w-3.5 h-3.5 text-emerald-400" />}
                        </div>
                        <div className="flex items-center gap-3 text-xs text-surface-500 mt-0.5">
                          <span>GW: {subnet.gateway}</span>
                          <span className="text-emerald-400">{subnet.statistics?.available || 0} IPs free</span>
                          {subnet.vlan && <span>VLAN: {subnet.vlan}</span>}
                        </div>
                      </div>
                      {!isAdded && <Plus className="w-4 h-4 text-surface-500" />}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    );
  };
  
  const renderCloudInitStep = () => (
    <div className="space-y-4">
      <h3 className="text-lg font-semibold text-surface-100">Access Configuration</h3>
      
      <SSHKeyPicker value={sshKey} onChange={setSshKey} />
      
      <div>
        <label className="block text-sm font-medium text-surface-300 mb-1">
          Initial Password
        </label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Enter password for default user"
          className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-md text-surface-100 placeholder-surface-500 focus:outline-none focus:ring-2 focus:ring-primary-500"
        />
        <p className="text-xs text-surface-500 mt-1">
          Optional: Set an initial password for the default user
        </p>
      </div>
      
      <div className="flex items-center gap-2">
        <input
          type="checkbox"
          id="startVM"
          checked={startVM}
          onChange={(e) => setStartVM(e.target.checked)}
          className="rounded bg-surface-800 border-surface-600"
        />
        <label htmlFor="startVM" className="text-sm text-surface-300">
          Start VM immediately after creation
        </label>
      </div>
    </div>
  );
  
  const renderReviewStep = () => {
    const names = generateVMNames();
    
    return (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-surface-100">Review & Create</h3>
        
        <div className="bg-surface-800 rounded-lg p-4 space-y-3">
          <div className="flex justify-between">
            <span className="text-surface-400">Project</span>
            <span className="text-surface-100 font-medium">{projects.find(p => p.name === selectedProject)?.display_name || selectedProject}</span>
          </div>
          {vmCount > 1 ? (
            <div>
              <div className="flex justify-between mb-2">
                <span className="text-surface-400">VMs to Create</span>
                <span className="text-surface-100 font-medium">{vmCount} VMs</span>
              </div>
              <div className="flex flex-wrap gap-1.5 ml-0">
                {names.slice(0, 6).map((n) => (
                  <span key={n} className="px-2 py-0.5 bg-surface-700 text-surface-200 rounded text-xs font-mono">{n}</span>
                ))}
                {names.length > 6 && (
                  <span className="px-2 py-0.5 text-surface-500 text-xs">+{names.length - 6} more</span>
                )}
              </div>
            </div>
          ) : (
            <div className="flex justify-between">
              <span className="text-surface-400">VM Name</span>
              <span className="text-surface-100 font-medium">{vmName}</span>
            </div>
          )}
          <div className="flex justify-between">
            <span className="text-surface-400">Template</span>
            <span className="text-surface-100">{selectedTemplate?.display_name}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-400">CPU</span>
            <span className="text-surface-100">{cpuCores} vCPU</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-400">Memory</span>
            <span className="text-surface-100">{memory}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-400">Disk Size</span>
            <span className="text-surface-100">{diskSize}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-400">SSH Key</span>
            <span className="text-surface-100">{sshKey ? 'Configured' : 'Not set'}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-400">Password</span>
            <span className="text-surface-100">{password ? 'Set' : 'Not set'}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-400">Network</span>
            <span className="text-surface-100">
              {nics.length === 0
                ? 'Pod Network (default)'
                : nics.map((n) => `${n.subnet}${n.staticIP ? ` (${n.staticIP})` : ''}`).join(', ')}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-surface-400">Start on Create</span>
            <span className="text-surface-100">{startVM ? 'Yes' : 'No'}</span>
          </div>
        </div>
        
        {batchProgress && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span className="text-surface-300">
                Creating {batchProgress.current}/{batchProgress.total}...
              </span>
              <span className="text-surface-400">
                {Math.round((batchProgress.current / batchProgress.total) * 100)}%
              </span>
            </div>
            <div className="w-full bg-surface-700 rounded-full h-2">
              <div
                className={`h-2 rounded-full transition-all ${batchProgress.failed.length > 0 ? 'bg-amber-500' : 'bg-primary-500'}`}
                style={{ width: `${(batchProgress.current / batchProgress.total) * 100}%` }}
              />
            </div>
            {batchProgress.failed.length > 0 && (
              <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-3 text-red-400 text-sm">
                <p className="font-medium mb-1">Failed to create {batchProgress.failed.length} VM(s):</p>
                <div className="flex flex-wrap gap-1">
                  {batchProgress.failed.map((n) => (
                    <span key={n} className="font-mono text-xs bg-red-500/20 px-1.5 py-0.5 rounded">{n}</span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
        
        {!batchProgress && createVM.error && (
          <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-3 text-red-400 text-sm">
            Failed to create VM. Please try again.
          </div>
        )}
      </div>
    );
  };
  
  const canProceed = () => {
    switch (step) {
      case 'template':
        return !!selectedTemplate && !!selectedProject;
      case 'customize': {
        if (!vmName || vmName.length < 2) return false;
        // Validate generated name is a valid k8s name
        const sampleName = vmName.replace(/\{n\}/g, '1');
        return /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(sampleName);
      }
      case 'network':
        return true; // Pod network (no NICs) is always valid; NICs are optional
      case 'cloudInit':
        return true;
      case 'review':
        return true;
      default:
        return false;
    }
  };
  
  const goNext = () => {
    const currentIdx = allSteps.indexOf(step);
    if (currentIdx < allSteps.length - 1) {
      setStep(allSteps[currentIdx + 1]!);
    }
  };
  
  const goBack = () => {
    const currentIdx = allSteps.indexOf(step);
    if (currentIdx > 0) {
      setStep(allSteps[currentIdx - 1]!);
    }
  };
  
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-surface-900 rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-700">
          <h2 className="text-xl font-semibold text-surface-100">Create Virtual Machine</h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-surface-800 rounded transition-colors"
          >
            <X className="w-5 h-5 text-surface-400" />
          </button>
        </div>
        
        {/* Content */}
        <div className="p-6 overflow-y-auto" style={{ maxHeight: 'calc(90vh - 140px)' }}>
          <div className="mb-6">
            <WizardStepIndicator
              steps={stepLabels}
              currentStep={allSteps.indexOf(step)}
            />
          </div>
          
          {step === 'template' && renderTemplateStep()}
          {step === 'customize' && renderCustomizeStep()}
          {step === 'network' && renderNetworkStep()}
          {step === 'cloudInit' && renderCloudInitStep()}
          {step === 'review' && renderReviewStep()}
        </div>
        
        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-surface-700">
          <button
            onClick={step === 'template' ? onClose : goBack}
            className="px-4 py-2 text-surface-400 hover:text-surface-200 transition-colors"
          >
            {step === 'template' ? 'Cancel' : 'Back'}
          </button>
          
          {step === 'review' ? (
            <button
              onClick={batchProgress && batchProgress.current >= batchProgress.total && batchProgress.failed.length > 0 ? onClose : handleSubmit}
              disabled={createVM.isPending || (batchProgress !== null && batchProgress.current < batchProgress.total)}
              className="px-6 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            >
              {batchProgress && batchProgress.current < batchProgress.total ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Creating {batchProgress.current}/{batchProgress.total}...
                </>
              ) : batchProgress && batchProgress.failed.length > 0 ? (
                'Close'
              ) : (
                <>
                  {createVM.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
                  {vmCount > 1 ? `Create ${vmCount} VMs` : 'Create VM'}
                </>
              )}
            </button>
          ) : (
            <button
              onClick={goNext}
              disabled={!canProceed()}
              className="px-6 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Next
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
