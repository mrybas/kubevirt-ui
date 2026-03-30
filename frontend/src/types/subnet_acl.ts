export interface SubnetAcl {
  action: 'allow-related' | 'allow' | 'drop' | 'reject';
  direction: 'from-lport' | 'to-lport';
  match: string;
  priority: number;
}

export interface SubnetAclListResponse {
  subnet: string;
  cidr_block: string;
  acls: SubnetAcl[];
  total: number;
}

export interface SubnetAclUpdateRequest {
  acls: SubnetAcl[];
}

export interface SubnetAclAddRequest {
  action: string;
  direction: string;
  match: string;
  priority: number;
}

export interface AclPresetTemplate {
  name: string;
  description: string;
  acls: SubnetAcl[];
}
