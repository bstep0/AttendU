
# AttendU - Facial Recognition Attendance System

An end‑to‑end web application that automates classroom attendance using facial recognition.

---

## Table of Contents
1. [Key Features](#key-features)  
2. [Tech Stack](#tech-stack)  
3. [Getting Started](#getting-started)  
4. [Local Development](#local-development)  
5. [Deployment](#deployment)    
6. [Authors & Acknowledgements](#authors--acknowledgements)

---

## Key Features
* **Fast face scanning** and ≥ 90 % recognition accuracy.
* **Role‑based dashboards** — Students, Teachers, and Admins each get scoped views.  
* **Firebase Firestore** persistence with server‑side rules.   

---

## Tech Stack
| Layer | Technology | Hosting |
|-------|------------|---------|
| Frontend | **React** JSX + Vite + CSS | **Firebase Hosting** |
| Backend | **Flask** REST API + DeepFace | **Local Server** |
| Face Recognition | **OpenCV + DeepFace** |
| Database | **Firebase Firestore** |
| Auth | Firebase Auth |

---

## Getting Started

### Prerequisites
* **Node.js** ≥ 18 LTS  
* **Python** ≥ 3.10  
* **Firebase CLI** ≥ 12  
* **Git**

### Clone & Bootstrap
```bash
git clone https://github.com/bstep0/AttendU.git
cd face-attendance
```

Create the following environment files:

**`backend/.env`**
```env
FIREBASE_CREDENTIALS=firebase_credentials.json
FIREBASE_PROJECT_ID=your-project-id
```

**`frontend/.env`**
```env
VITE_API_BASE=https://facialrecognitionattendancesystem.onrender.com/api/face-recognition
VITE_FIREBASE_API_KEY=***
VITE_FIREBASE_AUTH_DOMAIN=***
```

Install backend dependencies:
```bash
cd backend
python -m venv venv
source venv/bin/activate  # (or venv\Scripts\activate on Windows)
pip install -r requirements.txt
```

Install frontend dependencies:
```bash
cd frontend
npm install
```

---

## Local Development

### Backend
```bash
cd backend
flask --app app run --reload
```

### Frontend
```bash
cd frontend
npm run dev
```

---

## Firestore Attendance Schema

Manual rechecks now gatekeep each face scan for up to **45 minutes**. The backend writes the first result as a _pending_ attendance record so downstream dashboards must filter them out until staff complete the review.

| Field | Type | Description |
|-------|------|-------------|
| `studentID` | `string` | UID of the student who attempted the scan. |
| `classID` | `string` | Class identifier that the scan was submitted against. |
| `date` | `timestamp` | Central Time timestamp for when the scan hit the API. |
| `status` | `string` | **`"pending"`** until a reviewer promotes the record to `"Present"`, `"Late"`, etc. |
| `isPending` | `boolean` | Convenience flag; `true` while `status === "pending"`. Always filter these out of live attendance views. |
| `proposedStatus` | `string` | The status computed from the class schedule (e.g., `"Present"` or `"Late"`). Use this when the reviewer finalizes the record. |
| `createdAt` / `updatedAt` | `server timestamp` | Firestore server timestamps that track when the attempt was stored and last updated. |
| `pendingRecheckAt` | `timestamp` | When the 45-minute follow-up window expires. Frontends should keep the scan modal open until this time or until the record is finalized. |
| `networkEvidence` | `map` | Contains `remoteAddr`, `xForwardedFor`, `xRealIp`, `userAgent`, `forwardedProto`, and `requestId` captured from the request headers to support audit trails. |
| `verification` | `map` | DeepFace metrics such as `distance`, `threshold`, and `model` used for the comparison. |

### Display guidance for clients

1. **Hide pending records** in dashboards by checking `status !== "pending"` _or_ `isPending === false`.
2. **Show in-progress UI** while a user’s most recent record for the day is pending; the backend response includes `pending: true` and `recheck_due_at` for convenience.
3. **Only mark attendance complete** once staff (or an automated job) updates the document’s `status` and clears the `isPending` flag.

## Firestore Notifications Schema

Notifications are fanned out to banners, toasts, and inbox entries. Each document captures the common metadata along with a `surfaceTargets` list so clients know which surfaces should render the message.

| Field | Type | Description |
|-------|------|-------------|
| `userId` | `string` | Primary identifier for the targeted user (Firebase Auth UID or the `users` doc ID). |
| `userEmail` | `string` | Lowercased email address for the user; stored to support security rules when the doc ID is not the Auth UID. |
| `targets` | `array<string>` | All identifiers (UIDs and emails) authorized to read/update the notification. |
| `type` | `string` | Machine-readable notification category (e.g., `class-start-reminder`, `attendance-risk`). |
| `channel` | `string` | Immediate delivery surface for the document (`banner`, `toast`, or `inbox`). |
| `surfaceTargets` | `array<string>` | All surfaces that should ultimately display the event. Each surface gets its own document today. |
| `title` / `message` | `string` | User-facing content used by the UI components. |
| `tone` | `string` | Semantic tone for styling (`info`, `success`, `warning`, `error`). |
| `actionLabel` / `actionHref` | `string` | Optional CTA metadata for deep links. |
| `payload` | `map` | Structured payload (class IDs, attendance record IDs, counts, etc.) for client routing. |
| `toast` / `banner` | `map` | Surface-specific display hints (e.g., toast duration, banner persistence). |
| `read` | `boolean` | Set to `true` once the notification has been acknowledged. |
| `dismissedSurfaces` | `map` | Tracks per-surface dismissals with server timestamps. |
| `acknowledgedAt` | `timestamp` | Populated when the user confirms the notification via the callable endpoint. |
| `dedupeKey` | `string` | Hash combining the event, surface, and user to prevent duplicate delivery. |
| `createdAt` / `updatedAt` | `server timestamp` | Audit timestamps used for ordering and optimistic concurrency. |

Use the provided callable functions (`acknowledgeNotification`, `dismissNotificationSurface`) to update read state or dismissals without elevating client privileges.

---

## Deployment

### Cloud Functions & Scheduling

The Firebase Functions project (see `/frontend/functions`) includes three production triggers:

| Function | Trigger | Purpose |
|----------|---------|---------|
| `scheduleClassStartReminders` | Pub/Sub scheduler (`* * * * *`, Central Time) | Sends toast + inbox reminders to students when a class begins within five minutes. |
| `attendanceWriteHandler` | Firestore `attendance/{recordId}` writes | Emits absence risk alerts, and schedules decision-result jobs when a record is created in the `pending` state. |
| `attendanceDecisionQueue` | Cloud Tasks queue | Runs ~30 minutes after the pending record is created to deliver the final decision (banner/inbox for absences, toast/inbox otherwise). |

Deploy and test the functions from the `frontend/functions` directory:

```bash
cd frontend/functions
npm install
npm run build
npm test  # runs the unit suite covering the notification flows
firebase deploy --only functions
```

The scheduled task and task queue are provisioned automatically during deployment; no manual Scheduler or Cloud Tasks configuration is required beyond Firebase project setup.

### Backend → Render
1. **Create a new Web Service** in Render.  
2. **Connect** your GitHub repo and select the `/backend` directory as the root.  
3. **Environment:** `Python 3`, start command `python app.py`.  
4. Add environment variables from `backend/.env`.  
5. Every push to `main` automatically redeploys.

### Frontend → Firebase Hosting
```bash
# one‑time setup
firebase init hosting
# each deploy
npm run build
firebase deploy --only hosting
```

---

## Authors & Acknowledgements
| Name | Role |
|------|------|
| **Brendon Stepanek** |
| **Zain Jamal** | 
| **Joshua Odegai** |
| **Maximiliano Hernandez** |
| **Neel Patel** |

Special thanks to **Prof. Diana Rabah** for sponsorship and guidance.

---
