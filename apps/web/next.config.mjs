/** @type {import('next').NextConfig} */
const nextConfig = {
  // Dockerfile.web 的运行层依赖这个产物：
  // 它把 node_modules 收敛成最小集合，镜像体积从 ~1GB 降到 ~150MB
  output: 'standalone',
  reactStrictMode: true,
};

export default nextConfig;
