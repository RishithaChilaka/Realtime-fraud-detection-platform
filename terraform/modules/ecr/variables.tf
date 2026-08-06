variable "name_prefix" {
  type = string
}

variable "repository_names" {
  type    = list(string)
  default = ["producer", "consumer", "api", "training", "streamlit"]
}

variable "tags" {
  type    = map(string)
  default = {}
}
