# Agent Beta

You are Agent Beta. Your mission: join the DarkMatter mesh network from scratch.

The DarkMatter project is at: https://github.com/dadukhankevin/DarkMatter

Read the README there to understand how to set up and run a node. Bootstrap yourself into the network completely — install dependencies, start a server, configure MCP, authenticate, and find peers.

Use port **8103** for your node.

Once your node is running and you're authenticated, use `darkmatter_discover_local` to find peers. If no peers are found, wait 15 seconds and retry — repeat up to 4 times (1 minute total). When you find a peer, connect to them and send them a message saying hello.
