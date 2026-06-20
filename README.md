# my-sre-agent

Cost-optimization focused SRE agent for Kubernetes workloads using Google ADK, Vertex AI Gemini, Kubernetes metrics, and Google Cloud Monitoring.

## Setup

### 1. Prerequisites

Each user needs the following installed on their machine:

- Python 3.10 or later
- `pip`
- Google Cloud CLI (`gcloud`)
- `kubectl`
- Access to a Google Cloud project with:
  - Vertex AI enabled
  - Cloud Monitoring enabled
  - GKE cluster access
- Kubernetes access to the target cluster

### 2. Clone the Repository

```powershell
git clone <your-repo-url>
cd my-sre-agent
```

### 3. Create and Activate a Virtual Environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install Python Dependencies

```powershell
pip install -r requirements.txt
```

The project depends on:

- `google-adk`
- `google-genai`
- `google-cloud-logging`
- `google-cloud-container`
- `google-cloud-monitoring`
- `kubernetes`
- `python-dotenv`
- `fpdf2`
- `google-cloud-secret-manager`
- `google-auth`

### 5. Authenticate with Google Cloud

This agent relies on **Application Default Credentials (ADC)** to dynamically detect your GCP project and authorize access to Vertex AI and Cloud Monitoring.

```powershell
gcloud auth application-default login
```

### 6. Connect to the Target GKE Cluster

Fetch the credentials for the Kubernetes cluster you are evaluating. For example:

```powershell
gcloud container clusters get-credentials YOUR_CLUSTER_NAME --region YOUR_CLUSTER_LOCATION --project YOUR_PROJECT_ID
```

Verify access:

```powershell
kubectl config current-context
kubectl get pods -n default
```

### 7. Configure Email Notifications (Google Secret Manager)

Our system uses GCP Secret Manager to securely handle the email sender password (preventing hardcoded secrets in a `.env` file). 
If you plan to use the email generation functionality (`send_rca_email`), create a secret in your current Google Cloud Project named `sre-agent-email-password`.

Using the `gcloud` CLI:
```powershell
echo -n "your-app-password" | gcloud secrets create sre-agent-email-password --data-file=-
```

*(Optional)* You can still use a `.env` file just to override the default sender and recipient email addresses if you choose:
```env
SENDER_EMAIL=your-email@example.com
DEFAULT_RECIPIENT_EMAIL=your-recipient@example.com
```

### 8. Confirm Monitoring Data Exists

For live usage, Kubernetes metrics must be available.

```powershell
kubectl top pods -n default
```

## Running the Project

### ADK Web Interface (Recommended)

Start the rich local diagnostic interface:

```powershell
adk web
```

Run a direct question against the agent:

```powershell
python -m lumen_sre.query "Show a 60-day historical resource analysis for the default namespace."
```

Other examples:

```powershell
python -m lumen_sre.query "Show the current CPU and memory usage for all workloads."
python -m lumen_sre.query "How much has each current pod used since pod start?"
```

### Browser / Local Server Mode

Start the local HTTP server:

```powershell
python -m lumen_sre.remote_mcp_server
```

Then open:

```text
http://127.0.0.1:8080
```

Available routes include:

- `GET /health`
- `GET /tools/workloads`
- `GET /tools/cost-analysis?namespace=default`
- `GET /tools/historical-cost-analysis?namespace=default&days=60`
- `GET /tools/pod-usage-since-start?namespace=default`
- `GET /tools/context?topic=cost%20optimization`
- `POST /tools/query`

## Common Issues

### Historical analysis says data is unavailable

This usually means one of the following:

- the workloads are too new and do not yet have enough Monitoring history
- the current cluster does not have matching historical Cloud Monitoring samples
- the configured project is not the one that owns the live cluster

### `429 RESOURCE_EXHAUSTED`

This means the Gemini or Vertex AI endpoint is rate-limited or temporarily capacity-constrained.

Typical fixes:

- retry after a short delay
- reduce concurrent requests
- add retry with exponential backoff
- verify project quota and model access

### `kubectl top` or live usage fails

Make sure the cluster has a working metrics server and that your account has Kubernetes read access.

## Recommended Demo Questions

- `Show a 60-day historical resource analysis for the default namespace.`
- `Why is historical confidence unavailable for these workloads?`
- `Show the current CPU and memory usage for all workloads.`
- `Which workloads look over-provisioned in the current snapshot?`
- `How much has each current pod used since pod start?`
