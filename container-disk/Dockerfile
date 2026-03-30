# Stage 1: Download all packages and tools in Docker build (has network)
FROM ubuntu:24.04 AS downloader

ARG DEBIAN_FRONTEND=noninteractive
ARG K8S_VERSION=1.30
ARG K8S_PATCH=1.30.2

RUN apt-get update \
 && apt-get -y install \
     libguestfs-tools \
     linux-image-generic \
     wget \
     qemu-utils \
     curl \
     gpg \
     apt-transport-https \
 && apt-get clean

WORKDIR /build

# Download Ubuntu cloud image
RUN wget -O image.img https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

# Set up repos and download ALL debs for offline install
RUN mkdir -p /etc/apt/keyrings /build/debs \
 && curl -fsSL https://pkgs.k8s.io/core:/stable:/v${K8S_VERSION}/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${K8S_VERSION}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list \
 && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && cd /build/debs \
 && apt-get download \
    containerd.io \
    kubelet=${K8S_PATCH}-* \
    kubeadm=${K8S_PATCH}-* \
    kubectl=${K8S_PATCH}-* \
    socat conntrack ebtables \
    cri-tools \
    kubernetes-cni

# Download crictl and CNI plugins
RUN curl -fsSL -o /build/crictl.tar.gz https://github.com/kubernetes-sigs/cri-tools/releases/download/v${K8S_VERSION}.0/crictl-v${K8S_VERSION}.0-linux-amd64.tar.gz \
 && curl -fsSL -o /build/cni-plugins.tgz https://github.com/containernetworking/plugins/releases/download/v1.5.1/cni-plugins-linux-amd64-v1.5.1.tgz

# Create config files
RUN printf '%s\n' overlay br_netfilter > /build/modules-kubernetes.conf \
 && printf '%s\n' \
    net.bridge.bridge-nf-call-iptables=1 \
    net.bridge.bridge-nf-call-ip6tables=1 \
    net.ipv4.ip_forward=1 \
    net.ipv6.conf.all.forwarding=1 \
    net.ipv6.conf.all.disable_ipv6=0 \
    vm.overcommit_memory=1 \
    kernel.panic=10 \
    kernel.panic_on_oops=1 \
    fs.inotify.max_user_instances=8192 \
    fs.inotify.max_user_watches=524288 > /build/sysctl-kubernetes.conf

# Create install script to run inside guest
RUN cat > /build/install.sh << 'SCRIPT'
#!/bin/bash
set -ex
# Install debs (force-depends since we may not have all transitive deps — they exist in base image)
dpkg -i --force-depends /tmp/debs/*.deb || true
apt-get -f install -y || true
# Configure containerd
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i '/SystemdCgroup/ s/=.*/= true/' /etc/containerd/config.toml
# Install crictl
tar -xzf /tmp/crictl.tar.gz -C /usr/local/bin
# Install CNI plugins
mkdir -p /opt/cni/bin
tar -xzf /tmp/cni-plugins.tgz -C /opt/cni/bin
# Kernel modules config
mv /tmp/modules-kubernetes.conf /etc/modules-load.d/kubernetes.conf
# Sysctl config
mv /tmp/sysctl-kubernetes.conf /etc/sysctl.d/kubernetes.conf
# Disable unattended-upgrades and snapd
apt-get remove -y unattended-upgrades 2>/dev/null || true
apt-get remove -y snapd 2>/dev/null || true
# Enable services
systemctl enable kubelet.service
systemctl enable containerd.service
# Hold k8s packages
apt-mark hold kubelet kubeadm kubectl 2>/dev/null || true
# Clean
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/debs /tmp/crictl.tar.gz /tmp/cni-plugins.tgz /tmp/install.sh
# Restore resolv.conf for runtime
rm -f /etc/resolv.conf
ln -s ../run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
SCRIPT
RUN chmod +x /build/install.sh

# Stage 2: Customize image with virt-customize (no network needed)
FROM downloader AS builder

RUN qemu-img resize image.img 5G \
 && virt-customize -a image.img \
    --run-command "growpart /dev/sda 1 && resize2fs /dev/sda1" \
    --copy-in /build/debs:/tmp/ \
    --copy-in /build/crictl.tar.gz:/tmp/ \
    --copy-in /build/cni-plugins.tgz:/tmp/ \
    --copy-in /build/modules-kubernetes.conf:/tmp/ \
    --copy-in /build/sysctl-kubernetes.conf:/tmp/ \
    --copy-in /build/install.sh:/tmp/ \
    --run-command "chmod +x /tmp/install.sh && /tmp/install.sh"

# Stage 3: container disk image
FROM scratch
COPY --from=builder /build/image.img /disk/image.qcow2
