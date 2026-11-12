/**
 * Type aliases over the generated OpenAPI schema (src/api/schema.d.ts, `pnpm gen:api`).
 * Why: spec 11 - the client is generated from the one contract; no hand-written DTOs.
 * This file only names generated types; it defines no wire shape of its own.
 */
import type { components, operations } from './schema';

export type Schemas = components['schemas'];

export type ResponseMeta = Schemas['ResponseMeta'];
export type Warning = Schemas['Warning'];
export type Coverage = Schemas['Coverage'];
export type FreshnessState = Schemas['FreshnessState'];
export type QualityStatus = Schemas['QualityStatus'];
export type ReleasePointer = Schemas['ReleasePointer'];
export type AnalysisRelease = Schemas['AnalysisRelease'];
export type ReleaseComponent = Schemas['ReleaseComponent'];
export type Ranking = Schemas['Ranking'];
export type RankingEntry = Schemas['RankingEntry'];
export type SectorOverview = Schemas['SectorOverview'];
export type SectorRankEntry = Schemas['SectorRankEntry'];
export type CandidatePool = Schemas['CandidatePool'];
export type CandidatePoolItem = Schemas['CandidatePoolItem'];
export type Workspace = Schemas['Workspace'];
export type FeatureResult = Schemas['FeatureResult'];
export type Instrument = Schemas['Instrument'];
export type QuoteSnapshot = Schemas['QuoteSnapshot'];
export type BarWindow = Schemas['BarWindow'];
export type CanonicalBar = Schemas['CanonicalBar'];
export type AdjustedBar = Schemas['AdjustedBar'];
export type AdjustmentMode = Schemas['AdjustmentMode'];
export type Evidence = Schemas['Evidence'];
export type EvidenceDocument = Schemas['EvidenceDocument'];
export type EvidenceClaim = Schemas['EvidenceClaim'];
export type Community = Schemas['Community'];
export type CommunityObservation = Schemas['CommunityObservation'];
export type FundFlow = Schemas['FundFlow'];
export type FundFlowObservation = Schemas['FundFlowObservation'];
export type Seat = Schemas['Seat'];
export type SeatReport = Schemas['SeatReport'];
export type TradePlan = Schemas['TradePlan'];
export type PlanRef = Schemas['PlanRef'];
export type Condition = Schemas['Condition'];
export type Operand = Schemas['Operand'];
export type Strategy = Schemas['Strategy'];
export type StrategyInstance = Schemas['StrategyInstance'];
export type SourceHealth = Schemas['SourceHealth'];
export type Evaluation = Schemas['Evaluation'];
export type League = Schemas['League'];
export type PaperMethodCard = Schemas['PaperMethodCard'];
export type AccountProjection = Schemas['AccountProjection'];
export type AccountPosition = Schemas['AccountPosition'];
export type ActionabilitySnapshot = Schemas['ActionabilitySnapshot'];
export type ActionabilityEntry = Schemas['ActionabilityEntry'];
export type Layout = Schemas['Layout'];
export type LayoutUpdate = Schemas['LayoutUpdate'];
export type Watchlist = Schemas['Watchlist'];
export type WatchlistUpdate = Schemas['WatchlistUpdate'];
export type Annotation = Schemas['Annotation'];
export type AnnotationUpdate = Schemas['AnnotationUpdate'];
export type ExperimentRequest = Schemas['ExperimentRequest'];
export type JobReceipt = Schemas['JobReceipt'];
export type JobRun = Schemas['JobRun'];
export type ModelRun = Schemas['ModelRun'];
export type UpdatePage = Schemas['UpdatePage'];
export type UpdateNotification = Schemas['UpdateNotification'];
export type ErrorBody = Schemas['Error'];

/** Generic response envelope used by every operation: `{ meta, data }`. */
export interface Envelope<T> {
  meta: ResponseMeta;
  data: T;
}

export type OperationId = keyof operations;
