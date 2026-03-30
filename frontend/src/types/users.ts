export interface LLDAPUser {
  id: string;
  email: string;
  display_name: string;
  created?: string;
  groups: { id: number; display_name: string }[];
}

export interface LLDAPGroup {
  id: number;
  display_name: string;
  created?: string;
  member_count: number;
  members: { id: string; email: string; display_name: string }[];
}

export interface UserListResponse {
  items: LLDAPUser[];
  total: number;
  lldap_enabled: boolean;
}

export interface GroupListResponse {
  items: LLDAPGroup[];
  total: number;
  lldap_enabled: boolean;
}

export interface CreateUserRequest {
  id: string;
  email: string;
  display_name?: string;
  first_name?: string;
  last_name?: string;
  password: string;
}

export interface UpdateUserRequest {
  email?: string;
  display_name?: string;
  first_name?: string;
  last_name?: string;
}

export interface CreateGroupRequest {
  name: string;
}

export interface AddMemberRequest {
  user_id: string;
}
