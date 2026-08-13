import { env } from "cloudflare:workers";
import { Container, getContainer } from "@cloudflare/containers";

export class BoxciContainer extends Container {
  defaultPort = 8080;
  // Merge CI (including self-deploy) runs after the webhook returns; keep the
  // instance up long enough for nix build + wrangler deploy.
  sleepAfter = "2h";
  enableInternet = true;
  entrypoint = ["/bin/boxci-entrypoint"];
  envVars = {
    BOXCI_PORT: "8080",
    PATH: "/bin",
    HOME: "/var/lib/boxci",
    BOXCI_ROOT: "/var/lib/boxci",
    BOXCI_PUBLIC_URL: env.BOXCI_PUBLIC_URL || "https://boxci.latha.org",
    CLOUDFLARE_API_TOKEN: env.CLOUDFLARE_API_TOKEN || "",
    CLOUDFLARE_ACCOUNT_ID:
      env.CLOUDFLARE_ACCOUNT_ID || "2612967e82750619224e7446c4c41b0b",
  };
}

export default {
  async fetch(request, env) {
    const container = getContainer(env.BOXCI_CONTAINER, "boxci");
    return container.fetch(request);
  },
};
