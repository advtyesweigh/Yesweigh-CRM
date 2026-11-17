/**
 * Back-compat shim — prefer BusinessReportCard / extractBusinessReport.
 */
export {
  default,
  extractBusinessReport as extractSalesByDealer,
  extractBusinessReport,
  formatInr,
  formatQty,
  type BusinessReportData as SalesByDealerData,
  type BusinessReportData,
} from "./BusinessReportCard";
