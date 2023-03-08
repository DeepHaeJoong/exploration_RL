import gymnasium as gym
env = gym.make("MiniGrid-DoorKey-16x16-v0", render_mode="human")
print(env.observation_space)
print(env.action_space)

# env = gym.make("MiniGrid-BlockedUnlockPickup-v0", render_mode="human")


observation, info = env.reset(seed=42)
for _ in range(1000):
   action = env.action_space.sample()  # User-defined policy function
   observation, reward, terminated, truncated, info = env.step(action)

   if terminated or truncated:
      observation, info = env.reset()
env.close()