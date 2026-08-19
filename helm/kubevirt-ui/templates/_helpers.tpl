{{/*
Expand the name of the chart.
*/}}
{{- define "kubevirt-ui.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "kubevirt-ui.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "kubevirt-ui.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "kubevirt-ui.labels" -}}
helm.sh/chart: {{ include "kubevirt-ui.chart" . }}
{{ include "kubevirt-ui.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "kubevirt-ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubevirt-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "kubevirt-ui.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "kubevirt-ui.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Backend selector labels
*/}}
{{- define "kubevirt-ui.backend.selectorLabels" -}}
{{ include "kubevirt-ui.selectorLabels" . }}
app.kubernetes.io/component: backend
{{- end }}

{{/*
Frontend selector labels
*/}}
{{- define "kubevirt-ui.frontend.selectorLabels" -}}
{{ include "kubevirt-ui.selectorLabels" . }}
app.kubernetes.io/component: frontend
{{- end }}

{{/*
LLDAP secret name (supports existingSecret)
*/}}
{{- define "kubevirt-ui.lldapSecretName" -}}
{{- if .Values.lldap.existingSecret -}}
{{ .Values.lldap.existingSecret }}
{{- else -}}
{{ include "kubevirt-ui.fullname" . }}-lldap
{{- end -}}
{{- end }}

{{/*
The namespace frr-k8s runs in — one fact, two readers.

It names the Role that lets the backend write its FRRConfiguration and the
B3_FRR_NAMESPACE the backend reads to decide where to write it. A Role in one
namespace and a write to another grants nothing while looking correct on
review, so both come from here.

`backend.env` wins because that is where a site already sets it; `b3.frrNamespace`
is the chart's own default, and matches the fallback compiled into the backend.
*/}}
{{- define "kubevirt-ui.b3FrrNamespace" -}}
{{- $fromEnv := "" -}}
{{- if .Values.backend.env -}}
{{- $fromEnv = (.Values.backend.env.B3_FRR_NAMESPACE | default "") -}}
{{- end -}}
{{- $fromEnv | default .Values.b3.frrNamespace | default "o0-metallb" -}}
{{- end -}}
