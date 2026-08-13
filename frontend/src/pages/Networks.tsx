import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Plus } from 'lucide-react';
import { PageTitle } from '@/components/common/PageTitle';
import VPCs from './VPCs';
import { Network as UserNetworks } from './Network';
import { SystemNetworks } from './SystemNetworks';
import { Underlay } from './Underlay';

const TABS = [
  { id: 'vpcs', label: 'VPCs', subtitle: 'Virtual Private Clouds for network isolation' },
  { id: 'subnets', label: 'Subnets', subtitle: 'User-defined subnets and networks' },
  { id: 'underlay', label: 'Underlay', subtitle: 'Physical fabric VPC egress gateways attach to' },
  { id: 'system', label: 'System', subtitle: 'System-level Kube-OVN networks' },
] as const;

type TabId = typeof TABS[number]['id'];

export function Networks() {
  const [searchParams, setSearchParams] = useSearchParams();

  const rawTab = searchParams.get('tab') as TabId | null;
  const activeTab: TabId = TABS.some(t => t.id === rawTab) ? rawTab! : 'vpcs';

  // Single Create button → CreateNetworkWizard on Subnets tab. The wizard
  // itself has a type chooser at step 1 (VPC vs external/VLAN subnet), so
  // we don't need a per-type entry point on this page.
  const [createSubnetSignal, setCreateSubnetSignal] = useState(false);

  const setTab = (tab: TabId) => setSearchParams({ tab });

  const currentSubtitle = TABS.find(t => t.id === activeTab)?.subtitle ?? '';

  const handleCreate = () => {
    setTab('subnets');
    setCreateSubnetSignal(true);
  };

  return (
    <div className="space-y-4">
      <PageTitle title="Networks" subtitle={currentSubtitle}>
        <button onClick={handleCreate} className="btn-primary flex items-center gap-2">
          <Plus className="h-4 w-4" />
          Create
        </button>
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
        {activeTab === 'vpcs' && <VPCs />}
        {activeTab === 'subnets' && (
          <UserNetworks
            openCreate={createSubnetSignal}
            onCreateOpened={() => setCreateSubnetSignal(false)}
          />
        )}
        {activeTab === 'underlay' && <Underlay />}
        {activeTab === 'system' && <SystemNetworks />}
      </div>
    </div>
  );
}
