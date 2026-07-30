export type AuditSplit = "training" | "domain";
export type GroundTruthAssessment = "yes" | "almost" | "no";

export interface AuditAnswer {
  groundTruthAssessment: GroundTruthAssessment;
  exactMatchReasonable: boolean | null;
  failureDescription: string;
  updatedAt: string;
}

export interface AssignedTask {
  id: string;
  split: AuditSplit;
  poolIndex: number;
  category: string;
  sourcePath: string;
  instruction: string;
  answerPosition: string;
  assignmentOrder: number;
  audit: AuditAnswer | null;
}

export interface SplitProgress {
  split: AuditSplit;
  assigned: number;
  completed: number;
  remaining: number;
}

export interface DashboardResponse {
  email: string;
  progress: SplitProgress[];
  tasks: AssignedTask[];
}

export interface AuditorStat {
  email: string;
  split: AuditSplit;
  assigned: number;
  completed: number;
  remaining: number;
}

export interface TaskStat {
  taskId: string;
  split: AuditSplit;
  poolIndex: number;
  category: string;
  assigned: number;
  completed: number;
  groundTruthYes: number;
  groundTruthAlmost: number;
  groundTruthNo: number;
  groundTruthYesRate: number | null;
  groundTruthAlmostRate: number | null;
  groundTruthNoRate: number | null;
  exactMatchYes: number | null;
  exactMatchNo: number | null;
  exactMatchAverage: number | null;
}

export interface StatsResponse {
  auditors: AuditorStat[];
  tasks: TaskStat[];
  generatedAt: string;
}

export interface SubmitAuditRequest {
  groundTruthAssessment: GroundTruthAssessment;
  exactMatchReasonable: boolean | null;
  failureDescription: string;
}
