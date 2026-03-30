export interface ProfileResponse {
  email: string;
  ssh_public_keys: string[];
}

export interface UpdateSSHKeysRequest {
  ssh_public_keys: string[];
}
