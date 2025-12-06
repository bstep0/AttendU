
# Maintenance Manual for FRAS – 

## 1. Introduction
Our **Facial Recognition Attendance System  or FRAS** is a web application developed with the purpose of improving the efficiency of the classroom attendance-taking process using facial recognition.  

The system integrates:

- **Flask Backend** hosted using Render  
- **React Frontend** hosted using Firebase Hosting  
- **Firebase Firestore** for data storage  
- **DeepFace/OpenCV** for computer vision and facial recognition  

This manual was created to act as a good, detailed guide for system administrators and developers to use to maintain, monitor, and troubleshoot our application after FRAS' deployment.


## 2. System Requirements

### Hardware
- Computers for system maintenance and development
- Servers for testing
- Camera for testing

### Software
| Layer | Technology | Hosting |
|-------|-------------|----------|
| Frontend | React | Firebase Hosting |
| Backend | Flask REST API + DeepFace | Render |
| Facial Recognition | OpenCV + DeepFace | Local / Server |
| Database | Firebase Firestore | Cloud |
| Authentication | Firebase Auth | Cloud |

### Development Tools
- Python 3.10 +  
- Node.js 18 LTS +  
- Firebase CLI 12 +  
- Git and GitHub
- VS Code
- PyTest  



## 3. Maintenance Procedures

### 3.1 Backups
Backups are configured to automatically be made and stored daily. 

To view existing backups:
1. In Firebase  → Firestore Database → Disaster Recovery → Scheduled Backups → View All Backups.   
2. Verify integrity of backup.

### 3.2 Updates

#### Backend Deployment
```bash
cd backend
git add .
git commit -m "Backend maintenance update"
git push origin main
```
Render will automatically redeploy when push is made to GitHub.

#### Frontend Deployment
```bash
cd frontend
npm run build
firebase deploy --only hosting
```


### 3.3 Monitoring & Logging
- **Render Monitoring:** Enable uptime and error alerts.  
- **Firebase Performance:** Track API latency and Firestore response times.  
- **Logs:**  
  - Firebase → Data → 'errorLog' collection
 
- Review logs bi-weekly and be sure to maintain a changelog of all resolved issues.



### 3.4 Performance Testing
- Recalibrate DeepFace each quarter if necessary.  
- Attmept to optimize API endpoints and consider enabling caching for repeated requests.  
- Use lazy-loaded React components to reduce initial load time.  
- Conduct quarterly performance testing.


### 3.5 Security & Privacy Compliance
1. **HTTPS Required:** Enforce end-to-end encryption for all endpoints.  
2. **Access Control:** Quarterly review of Firebase Auth user roles.  
3. **Data Privacy:** Ensure Firestore rules restrict access by UID/email domain.  
4. **Data Modification:** Maintain logs for every attendance record modification
	4.1. Currently tracked showing the user who edited the record, the time of modification, and the reason.

## 4. Troubleshooting Guide

| Issue | Cause | Solution |
|--------|--------|-----------|
| **Face is not recognized** | Poor lighting or outdated model embeddings | Attempt to re-train DeepFace model with updated reference images. Consider switches models, or improve lighting conditions. |
| **Login failure** | Firebase Auth misconfigured or invalid credentials were entered | Verify correct input → Reauthenticate account |
| **Render backend offline** | Outage or redeploy failure | Restart service in Render → Dashboard → Manual Redeploy |
| **Data not saving to Firestore** | Permission issue | Check Firestore rules and ensure project limits not exceeded. |
| **Pending records never finalize** | Possible task misfire | Manually execute `attendanceDecisionQueue` and check logs. |
| **Frontend not loading** | Corrupted build | Rebuild (`npm run build`) and redeploy. |
| **Slow performance** | Increased traffic or unoptimized API | Profile queries; enable caching. |

## 5. Backup & Recovery Procedures

1. **Daily Complete Backup:** Export Firestore data 
2. **Verification:** Confirm record count + hash match between backup and production.  
3. **Restore Procedure:**  
   - Import data into Firestore.  
   - Validate schema.  
   - Swap after confirmation.  

## 6. Change Management Process

1. **Submit Change Request:** Document the modification's purpose and its impact.  
2. **Approval:** Must be reviewed by Project Manager, Lead Developer, and Stakeholders.  
3. **Testing:** Implement in developmentbranch, run complete tests.  
4. **Deployment:** Merge into `main`, conduct Render + Firebase redeploy.  

---

## 7. Appendices

### Appendix A – Firebase Firestore Database Schemas 
#### Attendance Collection
| Field | Type | Description |
|--------|------|-------------|
| `studentID` | string | UID of the student scanning in. |
| `classID` | string | Class identifier. |
| `date` | timestamp | Timestamp of the scan. |
| `status` | string | `"pending"`, `"Present"`, `"Absent"`|
| `isPending` | boolean | True while awaiting recheck. |
| `proposedStatus` | string | Automatically determined status based on schedule. |
| `pendingRecheckAt` | timestamp | 30-minute mark for automatic recheck. |
| `networkEvidence` | map | Request metadata for auditing. |
| `verification` | map | DeepFace model results (distance, threshold). |
| `createdAt / updatedAt` | server timestamp | Record history fields. |

#### Notifications Collection
| Field | Type | Description |
|--------|------|-------------|
| `userId` | string | Target UID or user doc ID. |
| `type` | string | Notification category (`class-start-reminder`, `attendance-risk`). |
| `channel` | string | Delivery surface (`toast`, `banner`, `inbox`). |
| `payload` | map | Structured data for routing. |
| `tone` | string | UI styling hint (`info`, `success`, `warning`, `error`). |
| `read` | boolean | True once user acknowledges. |
| `acknowledgedAt` | timestamp | Server time of acknowledgment. |
| `createdAt / updatedAt` | server timestamp | Standard audit fields. |

#### Users Collection
| Field | Type | Description |
|--------|------|-------------|
| `classes` | array | Assigned classes. |
| `email` | string | User's email. |
| `fname` | string | User's first name. |
| `lname` | string | User's last name. |
| `id` | string | User's UID. |
| `role` | string | User's assigned role. |

#### Classes Collection
| Field | Type | Description |
|--------|------|-------------|
| `id` | string | Class' ID. |
| `name` | string | Class name. |
| `room` | string | Class' assigned room. |
| `schedule` | string | Class scheduled days and times. |
| `students` | array | Students assigned to class. |
| `teacher` | string | Teacher assigned to class. |


### Appendix B – Deployment 
#### Backend → Render
1. Create → Web Service in Render.  
2. Link GitHub repo (`/backend` directory).  
3. Environment → Python 3.  
4. Start command:  
   ```bash
   python app.py
   ```
5. Add `.env` vars from Firebase.  
6. Push to `main` branch to trigger auto-deploy.

#### Frontend → Firebase Hosting
```bash
npm run build
firebase deploy --only hosting
```

### Appendix C – Useful Commands
```bash
# Deploy frontend
firebase deploy --only hosting

# Restart backend on Render
git push origin main

# Run backend tests
pytest

# Rebuild React app
npm run build
```
### Appendix D – Diagrams
![Class Diagram](https://github.com/bstep0/FRAS/blob/main/Revised%20Documentation%20For%20Implementation%20Phase/FRAS-ClassDiagram.png)
![Data Flow Diagram](https://github.com/bstep0/FRAS/blob/main/Revised%20Documentation%20For%20Implementation%20Phase/FRAS-DFD.png)
![Entity Relationship Diagram](https://github.com/bstep0/FRAS/blob/main/Revised%20Documentation%20For%20Implementation%20Phase/FRAS-ERD.png)
![Use Case Diagram](https://github.com/bstep0/FRAS/blob/main/Revised%20Documentation%20For%20Implementation%20Phase/FRAS-ERD.png)