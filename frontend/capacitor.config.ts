import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "in.yesweigh.crm",
  appName: "YesWeigh CRM",
  webDir: "dist",
  server: {
    androidScheme: "https",
    // For device builds, API is absolute via VITE_API_URL
  },
};

export default config;
