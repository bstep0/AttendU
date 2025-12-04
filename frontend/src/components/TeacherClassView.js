// TeacherClassView.js

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { doc, getDoc } from "firebase/firestore";
import { auth, db } from "../firebaseConfig";
import { useNotifications } from "../context/NotificationsContext";
import ClassAttendanceChart from "./ClassAttendanceChart";
import { EXPORT_ATTENDANCE_ENDPOINT } from "../config/api";
import TeacherLayout from "./TeacherLayout";
import {
  collectStudentIds,
  extractStudentId,
  fetchAttendanceDocuments,
} from "../utils/attendanceQueries";

const formatDateLabel = (date) =>
  date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });

const coerceToDate = (value) => {
  if (!value) return null;

  if (typeof value.toDate === "function") {
    const converted = value.toDate();
    if (!Number.isNaN(converted.getTime())) {
      return new Date(converted);
    }
  }

  if (value instanceof Date) {
    const converted = new Date(value);
    if (!Number.isNaN(converted.getTime())) {
      return converted;
    }
    return null;
  }

  if (typeof value === "number") {
    const converted = new Date(value);
    if (!Number.isNaN(converted.getTime())) {
      return converted;
    }
  }

  if (typeof value === "string") {
    const converted = new Date(value);
    if (!Number.isNaN(converted.getTime())) {
      return converted;
    }
  }

  if (value?.seconds) {
    const converted = new Date(value.seconds * 1000);
    if (!Number.isNaN(converted.getTime())) {
      return converted;
    }
  }

  return null;
};

const formatNameFromEmail = (email) => {
  if (typeof email !== "string") return "";

  const [localPart] = email.split("@");
  if (!localPart) return "";

  return localPart
    .split(/[._]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ")
    .trim();
};

const resolveStudentName = (studentData, fallbackName, fallbackId) => {
  if (studentData) {
    const {
      displayName,
      fullName,
      name,
      firstName,
      lastName,
      fname,
      lname,
      email,
    } = studentData;

    if (displayName) return displayName;
    if (fullName) return fullName;
    if (name) return name;

    const combined = [firstName || fname, lastName || lname]
      .filter(Boolean)
      .join(" ")
      .trim();
    if (combined) return combined;

    const emailName = formatNameFromEmail(email);
    if (emailName) return emailName;
  }

  if (fallbackName) {
    const formattedFallback = formatNameFromEmail(fallbackName) || fallbackName;
    if (formattedFallback.trim()) return formattedFallback;
  }

  if (fallbackId) return fallbackId;

  return "Unknown Student";
};

const TeacherClassView = () => {
  // URL param: /teacher/classes/:className
  const { className: classId } = useParams();

  const [attendanceRecords, setAttendanceRecords] = useState([]);
  const [classInfo, setClassInfo] = useState(null);
  const [enrolledStudents, setEnrolledStudents] = useState([]);
  const [rosterMap, setRosterMap] = useState(new Map());
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [isExporting, setIsExporting] = useState(false);
  const [exportFeedback, setExportFeedback] = useState(null);
  const [isRosterLoading, setIsRosterLoading] = useState(true);
  const { pushToast } = useNotifications();

  const fetchClassRoster = useCallback(async () => {
    setIsRosterLoading(true);
    if (!classId) {
      setClassInfo(null);
      setEnrolledStudents([]);
      setRosterMap(new Map());
      setIsRosterLoading(false);
      return;
    }

    try {
      const classSnapshot = await getDoc(doc(db, "classes", classId));
      if (!classSnapshot.exists()) {
        setClassInfo(null);
        setEnrolledStudents([]);
        setRosterMap(new Map());
        return;
      }

      const classData = classSnapshot.data() || {};
      setClassInfo({ id: classSnapshot.id, ...classData });

      const rawRoster =
        classData.students ||
        classData.studentIds ||
        classData.enrolledStudents ||
        classData.classList ||
        [];

      const normalizedRoster = [];
      const idsToLookup = new Set();

      rawRoster.forEach((entry) => {
        if (typeof entry === "string") {
          normalizedRoster.push({ id: entry, name: "" });
          idsToLookup.add(entry);
          return;
        }

        if (entry && typeof entry === "object") {
          const { id, studentID, studentId, name, fullName, displayName } = entry;
          const candidateId = id || studentID || studentId;
          if (candidateId) {
            idsToLookup.add(candidateId);
            normalizedRoster.push({
              id: candidateId,
              name: name || fullName || displayName || "",
            });
          }
        }
      });

      const rosterMapCopy = new Map();

      await Promise.all(
        Array.from(idsToLookup).map(async (studentId) => {
          try {
            const studentSnap = await getDoc(doc(db, "users", studentId));
            if (studentSnap.exists()) {
              rosterMapCopy.set(studentId, { id: studentSnap.id, ...studentSnap.data() });
            }
          } catch (error) {
            console.error(`Unable to load roster student ${studentId}`, error);
          }
        })
      );

      const resolvedRoster = normalizedRoster.map((student) => {
        const studentData = rosterMapCopy.get(student.id);
        return {
          id: student.id,
          name: resolveStudentName(studentData, student.name, student.id),
          data: studentData,
        };
      });

      resolvedRoster.sort((a, b) =>
        a.name.localeCompare(b.name, undefined, { sensitivity: "base" })
      );

      setEnrolledStudents(resolvedRoster);
      setRosterMap(rosterMapCopy);
    } catch (error) {
      console.error("Failed to load class roster", error);
      setClassInfo(null);
      setEnrolledStudents([]);
      setRosterMap(new Map());
      pushToast({
        tone: "error",
        title: "Roster unavailable",
        message: "We couldn't load the class roster. Please try again shortly.",
      });
    } finally {
      setIsRosterLoading(false);
    }
  }, [classId, pushToast]);

  const fetchAttendanceRecords = useCallback(async () => {
    if (!classId) {
      setAttendanceRecords([]);
      return;
    }

    try {
      const rawRecords = await fetchAttendanceDocuments(db, classId);

      const studentIds = collectStudentIds(rawRecords);

      const studentMap = new Map();
      studentIds.forEach((studentId) => {
        if (rosterMap.has(studentId)) {
          studentMap.set(studentId, rosterMap.get(studentId));
        }
      });

      const idsToFetch = studentIds.filter((studentId) => !studentMap.has(studentId));

      await Promise.all(
        idsToFetch.map(async (studentId) => {
          try {
            const studentRef = doc(db, "users", studentId);
            const studentSnapshot = await getDoc(studentRef);
            if (studentSnapshot.exists()) {
              const studentData = studentSnapshot.data();
              const studentName = resolveStudentName(studentData, null, studentSnapshot.id);

              studentMap.set(studentId, {
                id: studentSnapshot.id,
                ...studentData,
                name: studentName,
              });
            } else {
              studentMap.set(studentId, null);
            }
          } catch (error) {
            console.error("Failed to resolve student info", error);
            studentMap.set(studentId, null);
          }
        })
      );

      const enrichedRecords = rawRecords.map((record) => {
        const recordStudentId = extractStudentId(record);
        const studentData =
          studentMap.get(recordStudentId) || rosterMap.get(recordStudentId);
        const studentName = resolveStudentName(
          studentData,
          record.studentName || record.studentFullName,
          recordStudentId
        );

        const studentWithName = studentData
          ? studentData.name
            ? studentData
            : { ...studentData, name: studentName }
          : recordStudentId
          ? { id: recordStudentId, name: studentName }
          : null;

        const dateValue = coerceToDate(record.date);

        return {
          ...record,
          student: studentWithName,
          studentName,
          dateValue,
          formattedDate: dateValue ? formatDateLabel(dateValue) : "",
        };
      });

      enrichedRecords.sort((a, b) =>
        a.studentName.localeCompare(b.studentName, undefined, {
          sensitivity: "base",
        })
      );

      setAttendanceRecords(enrichedRecords);
    } catch (error) {
      console.error("Failed to load attendance records", error);
      pushToast({
        tone: "error",
        title: "Unable to load attendance",
        message:
          "We couldn't load the attendance records for this class. Please try again.",
      });
      setAttendanceRecords([]);
    }
  }, [classId, pushToast, rosterMap]);

  useEffect(() => {
    fetchClassRoster();
  }, [fetchClassRoster]);

  useEffect(() => {
    fetchAttendanceRecords();
  }, [fetchAttendanceRecords]);

  const attendanceSummary = useMemo(() => {
    const summary = { Present: 0, Absent: 0, Pending: 0 };

    if (!Array.isArray(attendanceRecords) || attendanceRecords.length === 0) {
      return summary;
    }

    attendanceRecords.forEach((record) => {
      const status =
        typeof record.status === "string" ? record.status.trim().toLowerCase() : "";

      if (!status) return;

      if (status.includes("pending")) {
        summary.Pending += 1;
        return;
      }

      if (status.includes("present")) {
        summary.Present += 1;
        return;
      }

      summary.Absent += 1;
    });

    return summary;
  }, [attendanceRecords]);

  const handleExportAttendance = async () => {
  setExportFeedback(null);

  if (!startDate || !endDate) {
    setExportFeedback({
      type: "error",
      message: "Select a start and end date before exporting attendance.",
    });
    return;
  }

  if (new Date(startDate) > new Date(endDate)) {
    setExportFeedback({
      type: "error",
      message: "The start date must be on or before the end date.",
    });
    return;
  }

  const user = auth.currentUser;
  if (!user) {
    setExportFeedback({
      type: "error",
      message: "You must be signed in as a teacher to export attendance records.",
    });
    return;
  }

  setIsExporting(true);

  try {
    // Build the same URL you pasted manually
    const url = new URL(EXPORT_ATTENDANCE_ENDPOINT);
    url.searchParams.set("classId", classId);
    url.searchParams.set("startDate", startDate);
    url.searchParams.set("endDate", endDate);

    // Let the browser handle the download directly (no fetch, no Authorization header)
    const link = document.createElement("a");
    link.href = url.toString();
    link.download = ""; // backend's Content-Disposition will set the filename
    document.body.appendChild(link);
    link.click();
    link.remove();

    setExportFeedback({
      type: "success",
      message:
        "Attendance export started. Check your downloads folder for the CSV file.",
    });
  } catch (error) {
    console.error("Attendance export failed:", error);
    setExportFeedback({
      type: "error",
      message:
        "We couldn't export attendance right now. Please verify your connection and try again.",
    });
  } finally {
    setIsExporting(false);
  }
};


  return (
    <TeacherLayout title={classId ? `${classId} Overview` : "Class Overview"}>
      <div className="space-y-6 text-gray-900 dark:text-slate-100">
        {/* Header actions */}
        <div className="flex items-center justify-between">
          <Link
            to="/teacher/classes"
            className="text-sm font-medium text-unt-green hover:text-unt-green dark:text-unt-green/90 dark:hover:text-unt-green"
          >
            ← Back to classes
          </Link>
        </div>

        {/* Attendance snapshot */}
        <section className="rounded-lg bg-white p-6 shadow-sm transition hover:border-unt-green/30 hover:shadow-brand dark:border-slate-700 dark:bg-slate-900">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-slate-100">
            Attendance Overview
          </h2>
          <p className="mt-2 text-sm text-gray-600 dark:text-slate-300">
            Review class-wide attendance at a glance.
          </p>
          <div className="mt-6 flex justify-start">
            <ClassAttendanceChart attendanceSummary={attendanceSummary} />
          </div>
        </section>

        {/* Student list */}
        <section className="rounded-lg bg-white p-6 shadow-sm transition hover:border-unt-green/30 hover:shadow-brand dark:border-slate-700 dark:bg-slate-900">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h2 className="mb-1 text-2xl font-semibold">Student List</h2>
              <p className="text-sm text-gray-600 dark:text-slate-300">
                Select a student to open their attendance dashboard and create records.
              </p>
            </div>
          </div>

          {isRosterLoading ? (
            <p className="mt-4 text-sm text-gray-500 dark:text-slate-400">
              Loading roster…
            </p>
          ) : enrolledStudents.length === 0 ? (
            <p className="mt-4 text-sm text-gray-500 dark:text-slate-400">
              No students are enrolled in this class yet.
            </p>
          ) : (
            <ul className="mt-5 space-y-3">
              {enrolledStudents.map((student) => (
                <li
                  key={student.id}
                  className="flex items-center justify-between gap-4 rounded-lg bg-gray-50 px-4 py-3 shadow-sm transition hover:shadow-md dark:bg-slate-800"
                >
                  <div>
                    <p className="text-lg font-semibold text-gray-900 dark:text-slate-100">
                      {student.name}
                    </p>
                    <p className="text-xs text-gray-500 dark:text-slate-400">
                      ID: {student.id}
                    </p>
                  </div>
                  <Link
                    to={`/teacher/classes/${classId}/students/${student.id}`}
                    className="rounded bg-unt-green px-3 py-1 text-md font-semibold text-white transition hover:bg-unt-green/90"
                  >
                    View Attendance
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* Export Attendance */}
        <section className="space-y-4 rounded-lg bg-white p-6 shadow-sm transition hover:border-unt-green/30 hover:shadow-brand dark:border-slate-700 dark:bg-slate-900">
          <h2 className="text-2xl font-semibold">Export Attendance</h2>
          <p className="text-sm text-gray-600 dark:text-slate-300">
            Choose the date range you would like to export. A CSV file will be generated with all
            records that match your selection.
          </p>

          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label
                className="mb-1 block text-sm font-medium text-gray-700 dark:text-slate-300"
                htmlFor="attendance-start-date"
              >
                Start date
              </label>
              <input
                id="attendance-start-date"
                type="date"
                className="w-full rounded border border-gray-300 px-3 py-2 text-gray-900 focus:border-blue-500 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
                value={startDate}
                max={endDate || undefined}
                onChange={(event) => setStartDate(event.target.value)}
              />
            </div>
            <div>
              <label
                className="mb-1 block text-sm font-medium text-gray-700 dark:text-slate-300"
                htmlFor="attendance-end-date"
              >
                End date
              </label>
              <input
                id="attendance-end-date"
                type="date"
                className="w-full rounded border border-gray-300 px-3 py-2 text-gray-900 focus:border-blue-500 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
                value={endDate}
                min={startDate || undefined}
                onChange={(event) => setEndDate(event.target.value)}
              />
            </div>
          </div>

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={handleExportAttendance}
              disabled={isExporting}
              className={`rounded bg-green-500 px-6 py-2 font-semibold text-white transition hover:bg-green-600 focus:outline-none focus:ring-2 focus:ring-green-400 focus:ring-offset-2 dark:bg-unt-green dark:hover:bg-unt-green/90 dark:focus:ring-unt-green/60 dark:focus:ring-offset-slate-900 ${
                isExporting ? "cursor-not-allowed opacity-60" : ""
              }`}
            >
              {isExporting ? "Exporting…" : "Export Attendance"}
            </button>
          </div>

          {exportFeedback && (
            <p
              className={`text-sm ${
                exportFeedback.type === "error"
                  ? "text-red-600 dark:text-red-400"
                  : "text-green-600 dark:text-green-400"
              }`}
            >
              {exportFeedback.message}
            </p>
          )}
        </section>
      </div>
    </TeacherLayout>
  );
};

export default TeacherClassView;
