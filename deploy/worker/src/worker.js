import { Container, getContainer } from "@cloudflare/containers";

export class BoxciContainer extends Container {
  // boxci-server listens on 8080
  defaultPort = 8080;
  sleepAfter = "30m";
  pingEndpoint = "localhost/health";
  entrypoint = ["boxci-server"];
  envVars = {
    BOXCI_PORT: "8080",
  };

  onStart() {
    console.log("Container started");
  }

  onStop(params) {
    console.log("Container stopped", JSON.stringify(params));
  }
}

export default {
  async fetch(request, env) {
    try {
      const container = getContainer(env.BOXCI_CONTAINER, "boxci");
      return await container.fetch(request);
    } catch (err) {
      return new Response(
        JSON.stringify({
          error: String(err),
          message: err?.message,
          name: err?.name,
          stack: err?.stack,
        }, null, 2),
        { status: 500, headers: { "content-type": "application/json" } }
      );
    }
  },
};
