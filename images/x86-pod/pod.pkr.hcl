packer {
  required_version = ">= 1.14.0"
  required_plugins {
    amazon = {
      version = "= 1.3.9"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "source_ami" {
  type        = string
  description = "Operator-reviewed Ubuntu 24.04 x86_64 AMI in us-west-2."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.source_ami))
    error_message = "Pin an AMI ID; image-name searches are not supported."
  }
}

variable "subnet_id" { type = string }
variable "security_group_id" {
  type        = string
  description = "Existing group admitting private SSH from the bake controller only."
}
variable "payload_archive" { type = string }
variable "payload_sha256" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.payload_sha256))
    error_message = "Supply the reviewed payload SHA-256."
  }
}

variable "cloud_build_authorized" {
  type        = bool
  default     = false
  description = "Set only after the coordinator confirms the hub PR and authorizes a live bake."
  validation {
    condition     = var.cloud_build_authorized
    error_message = "Cloud baking is on hold. Obtain coordinator confirmation of the hub PR first."
  }
}

source "amazon-ebs" "pod" {
  region                      = "us-west-2"
  instance_type               = "m7i.large"
  source_ami                  = var.source_ami
  ssh_username                = "ubuntu"
  ssh_timeout                 = "10m"
  ami_name                    = "pod-x86-{{timestamp}}"
  ami_description             = "Headless x86 pod with verified tools and dependency caches"
  security_group_id           = var.security_group_id
  subnet_id                   = var.subnet_id
  associate_public_ip_address = false
  encrypt_boot                = true
  imds_support                = "v2.0"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_type           = "gp3"
    volume_size           = 40
    delete_on_termination = true
    encrypted             = true
  }
  tags = {
    Name         = "pod-x86"
    Architecture = "x86_64"
    Payload      = var.payload_sha256
  }
}

build {
  sources = ["source.amazon-ebs.pod"]
  provisioner "shell" {
    inline = ["cloud-init status --wait", "mkdir -p /tmp/pod-image"]
  }
  provisioner "file" {
    source      = "${path.root}/"
    destination = "/tmp/pod-image/"
  }
  provisioner "file" {
    source      = var.payload_archive
    destination = "/tmp/pod-payload.tar.gz"
  }
  provisioner "shell" {
    environment_vars = ["POD_PAYLOAD_SHA256=${var.payload_sha256}"]
    execute_command  = "sudo -n env {{ .Vars }} bash '{{ .Path }}'"
    script           = "${path.root}/install.sh"
  }
}
