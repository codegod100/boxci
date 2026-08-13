import { Container, getContainer } from "@cloudflare/containers";

export class BoxciContainer extends Container {
  // boxci-server listens on 8080
  defaultPort = 8080;
  sleepAfter = "30m";
  pingEndpoint = "localhost/health";
}

export default {
  async fetch(request, env) {
    const container = getContainer(env.BOXCI_CONTAINER, "boxci");
    return container.fetch(request);
  },
};
