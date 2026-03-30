import { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Plus, ChevronDown } from 'lucide-react';
import { PageTitle } from '@/components/common/PageTitle';
import VPCs from './VPCs';
import { Network as UserNetworks } from './Network';
import { SystemNetworks } from './SystemNetworks';

const TABS = [
  { id: 'vpcs', label: 'VPCs', subtitle: 'Virtual Private Clouds for network isolation' },
  { id: 'subnets', label: 'Subnets', subtitle: 'User-defined subnets and networks' },
  { id: 'system', label: 'System', subtitle: 'System-level Kube-OVN networks' },
] as const;

type TabId = typeof TABS[number]['id'];

export function Networks() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [createOpen, setCreateOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const rawTab = searchParams.get('tab') as TabId | null;
  const activeTab: TabId = TABS.some(t => t.id === rawTab) ? rawTab! : 'vpcs';

  // Read create signal from URL and clear it
  const createParam = searchParams.get('create');
  const [createVpcSignal, setCreateVpcSignal] = useState(false);
  const [createSubnetSignal, setCreateSubnetSignal] = useState(false);

  useEffect(() => {
    if (createParam === 'vpc') {
      setCreateVpcSignal(true);
      setSearchParams({ tab: 'vpcs' });
    } else if (createParam === 'subnet') {
      setCreateSubnetSignal(true);
      setSearchParams({ tab: 'subnets' });
    }
  }, [createParam]);

  const setTab = (tab: TabId) => setSearchParams({ tab });

  const currentSubtitle = TABS.find(t => t.id === activeTab)?.subtitle ?? '';

  // Close dropdown on outside click
  useEffect(() => {
    function handle(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setCreateOpen(false);
      }
    }
    if (createOpen) document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [createOpen]);

  const handleCreateVPC = () => {
    setCreateOpen(false);
    setTab('vpcs');
    setCreateVpcSignal(true);
  };

  const handleCreateSubnet = () => {
    setCreateOpen(false);
    setTab('subnets');
    setCreateSubnetSignal(true);
  };

  return (
    <div className="space-y-4">
      <PageTitle title="Networks" subtitle={currentSubtitle}>
        <div className="relative" ref={dropdownRef}>
          <button
            onClick={() => setCreateOpen(!createOpen)}
            className="btn-primary flex items-center gap-2"
          >
            <Plus className="h-4 w-4" />
            Create
            <ChevronDown className="h-3.5 w-3.5" />
          </button>
          {createOpen && (
            <div className="absolute right-0 top-full mt-1 w-44 bg-surface-900 border border-surface-700 rounded-lg shadow-xl z-20">
              <button
                onClick={handleCreateVPC}
                className="w-full text-left px-4 py-2.5 text-sm text-surface-200 hover:bg-surface-800 rounded-t-lg transition-colors"
              >
                Create VPC
              </button>
              <button
                onClick={handleCreateSubnet}
                className="w-full text-left px-4 py-2.5 text-sm text-surface-200 hover:bg-surface-800 rounded-b-lg transition-colors"
              >
                Create Subnet
              </button>
            </div>
          )}
        </div>
      </PageTitle>

      {/* Tabs */}
      <div className="border-b border-surface-800 flex">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setTab(tab.id)}
            className={`px-4 py-2.5 text-sm font-medium transition-colors ${
              activeTab === tab.id
                ? 'text-white border-b-2 border-primary-600 -mb-px'
                : 'text-surface-400 hover:text-surface-200'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div>
        {activeTab === 'vpcs' && (
          <VPCs
            openCreate={createVpcSignal}
            onCreateOpened={() => setCreateVpcSignal(false)}
          />
        )}
        {activeTab === 'subnets' && (
          <UserNetworks
            openCreate={createSubnetSignal}
            onCreateOpened={() => setCreateSubnetSignal(false)}
          />
        )}
        {activeTab === 'system' && <SystemNetworks />}
      </div>
    </div>
  );
}
